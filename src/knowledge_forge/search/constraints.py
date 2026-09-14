"""Query Understanding → 구조화된 제약(QueryConstraints) 추출 - wiki/search-architecture.md
"① Query Understanding"의 명시적 레이어, wiki/query-types.md의 검색 유형 표를 실제 코드로
잡아내는 곳.

지금까지 "이 질의가 특정 문서를 가리키는가 / 작성자·기간 필터인가 / 개수를 묻는가 / 비교인가"
같은 판단은 전부 에이전트(LLM)가 매 턴 암묵적으로 했다. 그걸 굳이 결정론적인 별도 단계로 뽑아낸
이유는 두 가지다. (a) 같은 입력에 같은 출력이 나와야 검색 품질을
재현 가능하게 측정하고 비교할 수 있다. (b) 검색 유형 표(Navigational/Temporal/
Aggregation/Exhaustive/Exact/Negative/Comparison/...)의 절반 이상은 "유사도 검색으로는 원리적으로
못 잡는" 유형이라, 검색 전략 선택 자체가 질의를 제약+의도 플래그로 먼저 분해하는 데 달려 있다.

2단계로 나눈 이유:
- 1단계 `extract_constraints_rules()`: LLM 없이 regex/사전만으로. 시간 범위(`search/temporal.py`
  재사용), 따옴표/식별자/파일명, 최신/전부/개수/요약/전체/부정/비교 마커, 작성자 조사 패턴,
  소스·문서 타입 힌트. 이 결과는 결정론적이라 단위 테스트로 못
  박을 수 있다. `search/router.py`/`temporal.py`가 LLM 대신 regex를 택한 것과 같은 이유 -
  "어제"를 날짜 범위로, `ERR_CONNECTION_RESET`을 정확 일치 토큰으로 바꾸는 건 기계적 계산이지
  판단이 아니다.
- 2단계 `extract_constraints()`: `ChatProvider`가 주어지면 규칙 결과를 프롬프트에 실어 보내
  entities/authors/document_reference/document_types/semantic_query/is_multihop/comparison_targets
  **만** 보강한다. 시간 범위·식별자·의도 플래그는 규칙 결과를 신뢰하고 LLM이 덮어쓰지 못한다 -
  LLM이 "2025년 3월"을 다른 달로 바꾸거나 에러코드를 "정정"하는 사고를 구조적으로 막기 위해서.
  JSON 파싱 실패/예외 시 규칙 결과로 graceful degrade(`search/planner.py::decompose_query`와 같은
  태도 - 보강 실패가 검색 자체를 막으면 안 된다).

`suggest_strategies()`가 내놓는 전략 이름 목록은 다음 단계 통합 실행기(`search/retrieve.py`)와의
계약이다 - 그 함수의 docstring에 각 이름의 의미를 적어 두었다.

이 모듈은 `search/service.py`/`planner.py`를 import하지 않는다 - DB/임베딩 없이
제약 추출만 단독으로 돌릴 수 있어야 하고, `tests/search/test_layering.py`가 지키는 "search/*는
agent/* 아래" 계층도 그대로 유지하기 위해서(`ChatProvider`는 `knowledge_forge.chat_provider`에서
가져온다).

알려진 한계: 마커 사전은 한국어/영어 표면형 기반이라 "정리"(요약)/"없는"(부정)/"최근"(최신)처럼
다의적인 표현은 오탐이 있다. 규칙 단계는 recall 우선(플래그를 켜는 쪽), 잘못 켜진 플래그의 비용은
검색 전략 하나를 더 도는 정도라 `router.py`의 "틀려도 소스 하나 더 조회할 뿐" 원칙과 같다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from knowledge_forge.chat_provider import ChatProvider, Message
from knowledge_forge.search.temporal import (
    RECENT_QUANTITY_PATTERN,
    RECENT_SPAN_PATTERN,
    TimeRange,
    parse_temporal_expression,
)

logger = logging.getLogger(__name__)

ORIGIN_RULE = "rule"
ORIGIN_LLM = "llm"

# --- 전략 이름 (search/retrieve.py 계약) -------------------------------------

STRATEGY_DOCUMENT_LOOKUP = "document_lookup"
STRATEGY_LEXICAL = "lexical"
STRATEGY_METADATA_FILTER = "metadata_filter"
STRATEGY_VECTOR = "vector"
# 어휘 arm(`retrieve.py::_Run.keyword_arm`) - semantic_query를 제목/본문에도 거는 하이브리드의
# 한쪽 팔. 원래는 `vector()` 끝에서 무조건 호출되는 부작용이라 전략 목록에 이름이 없었고, 그
# 탓에 `strategies_override=["vector"]`가 "순수 벡터"라는 문서상의 약속을 지키지 못했다(실제로는
# vector + arm이 돌아 A/B의 벡터 단독 arm이 통째로 무효였다 - "vector 단독 0.736"으로 기록된
# v0 ndcg@10은 실제로는 vector+arm의 값이고, 진짜 벡터 단독은 0.477이다). 이름을 주어 "arm 없는
# vector"와 "vector 없는 arm"을 override로 표현할 수 있게 한 것이지, 기본 라우팅 동작은 그대로다 -
# `suggest_strategies`가 vector를 넣을 때 항상 같이 넣는다.
STRATEGY_KEYWORD_ARM = "keyword_arm"
STRATEGY_LATEST = "latest"
STRATEGY_EXHAUSTIVE = "exhaustive"
STRATEGY_COUNT = "count"
STRATEGY_WHOLE_DOCUMENT = "whole_document"
STRATEGY_COMPARISON = "comparison"
STRATEGY_NEGATIVE = "negative"
STRATEGY_MULTIHOP = "multihop"

@lru_cache(maxsize=512)
def _latin_aware(pattern: str) -> str:
    r"""패턴 안의 `\b`를 **라틴 낱말 경계**로 바꾼다 - 뒤/앞에 한글이 바로 붙어도 통과한다.

    파이썬 `re`의 `\b`는 유니코드 인식이라 **한글도 `\w`다.** 그래서 "Google Drive에서"의
    `e`와 `에` 사이에는 경계가 없고, `\bg?drive\b`가 통째로 실패한다. 한국어 질의에서 영어
    고유명사 뒤에는 조사가 거의 항상 붙으므로("Drive에서" / "Slack에" / "Notion의")
    이건 예외가 아니라 **기본형**이다.

    실측(v1 골든셋): "Google Drive에서 수집된 엑셀(xlsx) 파일 전부"는 `source_types`가
    비어 metadata_filter가 아예 안 걸렸고(ndcg 0.000), 조사가 없는 "Google Drive 엑셀
    파일 전부"는 같은 코드에서 `['google_drive']`가 나왔다. 차이는 조사 하나뿐이었다.

    바꾸는 방향은 **한쪽뿐**이다 - 라틴 문자/숫자가 붙는 경우는 `\b`와 똑같이 막고
    (`install`이 `\ball\b`에 안 걸리는 성질은 그대로다), 한글·기호가 붙는 경우만 새로
    통과시킨다. 그래서 이 변환은 매치를 **늘리기만** 하고 줄이지 않는다.

    여는 쪽/닫는 쪽 판정은 `\b` 바로 뒤 문자로 한다 - 영숫자면 낱말이 시작되는 자리다.
    이 모듈의 마커는 전부 `\b<라틴 낱말>\b` 꼴이라 이 규칙으로 갈린다."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("\\b", i):
            nxt = pattern[i + 2] if i + 2 < len(pattern) else ""
            out.append("(?<![A-Za-z0-9])" if nxt.isalnum() else "(?![A-Za-z0-9])")
            i += 2
        else:
            out.append(pattern[i])
            i += 1
    return "".join(out)


# --- 마커 사전 (모듈 상수 - 표면형 추가는 여기만 고치면 된다) -------------------
# 각 항목은 regex 문자열. 한국어는 어절 경계가 불규칙해서 \b를 쓰지 않고, 조사/어미가 붙어도
# 잡히도록 어간만 적는다. 영어는 \b로 단어 경계를 건다 - 단, 그 \b는 `_latin_aware`를 거쳐야
# 한다(바로 아래 주석 참고). 마커에 \b를 새로 적을 때 그 함수를 안 거치면 조용히 안 잡힌다.

LATEST_MARKERS: tuple[str, ...] = (
    r"가장\s*최근",
    r"제일\s*최근",
    # "최근 3개월"/"최근 일주일"은 temporal(범위)이지 latest(정렬)가 아니므로 수량이 뒤따르는
    # 경우는 제외한다. 예전엔 아라비아 숫자만 제외해서 "최근 일주일"이 latest로도 잡혔다 -
    # 정렬로 상위 몇 건만 남기면 set query(v0-relative_temporal-003)가 깨진다.
    rf"최근(?!\s*(?:{RECENT_QUANTITY_PATTERN}))",
    r"최신",
    r"마지막",
    r"\blatest\b",
    r"\bmost\s+recent\b",
    r"\bnewest\b",
)
EXHAUSTIVE_MARKERS: tuple[str, ...] = (
    r"전부",
    r"모두",
    r"모든",
    r"빠짐없이",
    r"전체\s*(?:목록|리스트)",
    r"목록",
    r"리스트",
    # "다"는 단독 어절일 때만 - "다른", "보고서다" 등에 붙지 않도록
    r"(?:^|\s)다(?=\s|$)",
    r"\ball\b",
    r"\bevery\b",
    r"\blist\b",
)
COUNT_MARKERS: tuple[str, ...] = (
    # 한국어는 세는 대상마다 분류사가 달라진다 - "메일 몇 통"(v0-aggregation-002)을 놓쳐
    # count 전략이 아예 안 뽑혔다(predicted_count=None). 넣지 않은 것도 실측 근거가 있다:
    # 시간 단위(년/달/주/일)는 "몇 년 전"처럼 시점 표현이고, "팀"/"가지"는 코퍼스 개수가 아니라
    # 문서 안에 적힌 사실을 묻는 쪽이 많다(`v0-factoid-004` "총 몇 팀으로 구성되어 있나?").
    r"몇\s*(?:개|건|명|번|회|종류|통|장|부|곳|군데|권|편)",
    r"개수",
    r"건수",
    r"수량",
    r"\bcount\b",
    r"\bhow\s+many\b",
    r"\bnumber\s+of\b",
)
SUMMARY_MARKERS: tuple[str, ...] = (
    r"요약",
    r"정리",
    r"개요",
    r"핵심\s*내용",
    r"\bsummar(?:y|ize|ise)\b",
    r"\btl;?dr\b",
)
WHOLE_DOCUMENT_MARKERS: tuple[str, ...] = (
    r"전문",
    r"원문",
    r"통째로",
    r"처음부터\s*끝까지",
    r"전체\s*(?:내용|문서|텍스트)",
    r"(?:내용|문서)\s*전체",
    r"\bwhole\b",
    r"\bentire\b",
    r"\bfull\s+text\b",
)
NEGATIVE_MARKERS: tuple[str, ...] = (
    r"언급\s*(?:되지\s*않|안\s*된|없)",
    r"나오지\s*않",
    r"포함\s*(?:되지\s*않|안\s*된)",
    r"없는",
    r"빠진",
    r"누락",
    r"제외",
    r"\bnot\s+mentioned\b",
    r"\bmissing\b",
    r"\bwithout\b",
    r"\bexcluding\b",
    r"\bexcept\b",
)
COMPARISON_MARKERS: tuple[str, ...] = (
    r"차이",
    r"비교",
    r"대비",
    r"달라진",
    r"변경\s*점",
    r"변화",
    r"\bvs\.?\b",
    r"\bversus\b",
    r"\bcompar(?:e|ison)\b",
    r"\bdifferen(?:ce|t)\b",
)
# 규칙 단계의 multi-hop은 "추정"이다 - 앞 검색 결과를 알아야 뒤 검색이 성립하는 대명사/한정
# 표현만 잡고, 진짜 분해는 search/planner.py가 한다.
MULTIHOP_MARKERS: tuple[str, ...] = (
    r"그\s*중",
    r"중에서",
    r"그\s*(?:프로젝트|사람|담당자|문서|회의)의",
    r"그\s*이유",
    r"왜\s.*(?:했는지|됐는지|인지)",
    r"담당자가\s.*(?:작성|참여|보낸)",
    r"작성한\s*사람(?:이|의)",
)

# 소스 타입 힌트 → sources.source_type 값(local_file/slack/gmail/google_drive/notion)
#
# 이 정규식은 두 곳에서 쓰인다: (1) source_type 판정, (2) `_strip_source_type_markers`가
# semantic_query에서 마커 문구를 걷어내는 것. (2) 때문에 **매치 범위는 "지워도 되는 구절 전체"여야
# 한다** - 반쪽만 지우면 판정에도 검색에도 도움이 안 되는 파편이 남는다. "구글 메일"에서 "메일"만
# 지우고 "구글"을 남기지 않도록 두 단어 형태를 alternation 앞에 둔 이유가 그것이다(파이썬 `|`는
# leftmost-first라 긴 쪽이 앞에 있어야 먼저 소비된다). "Google Drive"도 같은 파편 문제가 있었지만
# 그쪽은 아예 지우지 않는 쪽이 낫다 - `_MARKER_KEEP_PATTERN` 주석 참고.
#
# 주의: 이 어휘 → source_type 매핑은 **질의 표면형 추측일 뿐이고 방향이 자주 반대다.** 개발
# 코퍼스 실측으로 본문에 "Google Drive"/"드라이브"를 언급하는 문서의 source_type은 gmail 11건 /
# slack 4건 / google_drive 3건이었다. 그래서 `search/retrieve.py`는 이 힌트만으로 만들어진 필터를
# 하드 `WHERE IN`으로 쓰지 않는다(`_is_source_hint_only_filter`).
SOURCE_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"슬랙|\bslack\b|채널|스레드|\bDM\b", "slack"),
    (r"(?:구글|google)\s*(?:메일|g?mail)\b|이메일|메일|\bg?mail\b|\be-?mail\b|수신함", "gmail"),
    (r"드라이브|\bg?drive\b|구글\s*(?:독스|문서|시트)|스프레드시트", "google_drive"),
    (r"노션|\bnotion\b", "notion"),
    (r"로컬\s*파일|\blocal\s+file", "local_file"),
)
# 파일 종류 힌트 → `sources.metadata->>'name'`의 확장자.
#
# **왜 mimeType이 아니라 확장자인가.** 둘 다 코퍼스에 있지만 뜻이 다르다 - Google Sheets
# 네이티브 문서도 mimeType이 spreadsheetml.sheet라, "xlsx 파일"을 mimeType으로 잡으면
# 업로드된 .xlsx(21건)에 네이티브 시트까지 섞여 23건이 된다. 사용자가 "xlsx 파일"이라고
# 할 때 가리키는 건 파일명이다. 반대로 mimeType을 **질의가 직접 말한 경우**
# ("mimeType이 text/markdown으로...")는 그대로 mimeType으로 잡아야 한다 - 확장자가 .md여도
# mimeType이 text/plain인 문서가 따로 있어서 확장자로는 못 가른다.
FILE_EXTENSION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"엑셀|\bxlsx?\b|스프레드시트\s*파일", (".xlsx", ".xls")),
    (r"\bcsv\b", (".csv",)),
    (r"파워포인트|프레젠테이션\s*파일|\bpptx?\b", (".pptx", ".ppt")),
    (r"워드\s*파일|\bdocx?\b", (".docx", ".doc")),
    (r"\bpdf\b", (".pdf",)),
    (r"한글\s*파일|\bhwpx?\b", (".hwpx", ".hwp")),
)
# "mimeType이 text/markdown", "mime type: application/pdf" - 질의가 MIME 타입을 그대로 적은 경우.
_MIME_TYPE_RE = re.compile(
    r"mime\s*_?type\s*(?:이|가|는|은|:|=)?\s*([a-z]+/[a-z0-9.+\-]+)", re.IGNORECASE
)

# 문서 타입 힌트 → 정규화된 한국어 라벨(sources.metadata의 name/subject/title에 lexical로 대거나
# LLM이 같은 라벨로 보강할 수 있게 고정 어휘로 둔다).
DOCUMENT_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"회의록|미팅\s*노트|회의\s*노트|\bmeeting\s+notes?\b|\bminutes\b", "회의록"),
    (r"사업\s*계획|계획서|\bplan\b", "계획서"),
    (r"보고서|리포트|\breport\b", "보고서"),
    (r"제안서|\bproposal\b", "제안서"),
    (r"계약서|\bcontract\b", "계약서"),
    (r"명세서|스펙|\bspec(?:ification)?\b", "명세서"),
    (r"매뉴얼|가이드|\bmanual\b|\bguide\b", "매뉴얼"),
    (r"발표\s*자료|슬라이드|\bppt\b|프레젠테이션|\bdeck\b", "발표자료"),
    (r"이메일|메일|\be-?mail\b", "메일"),
)

# 시간 표현 표면형 - 의미 해석은 전부 temporal.py가 하고, 여기서는 semantic_query에서 그 구절을
# 걷어내는 용도로만 쓴다(temporal.py는 매치 위치를 돌려주지 않는다). 올해/작년은 temporal.py에
# 없어 `_supplemental_time_range`로 여기서 보충한다.
_TEMPORAL_SPAN_RE = re.compile(
    r"\d{4}\s?년\s?\d{1,2}\s?월(?:\s?\d{1,2}\s?일)?(?:에|의|부터|까지)?"
    r"|\d{4}\s?(?:년)?\s*(?:[Qq]\d|\d\s?분기)(?:에|의)?"
    # 수량 표면형은 temporal.py가 단일 출처 - 숫자/한글 수사 목록이 두 곳으로 갈라지면
    # semantic_query에 "일주일"만 남는 식으로 반드시 어긋난다.
    # 뒤따르는 "간/동안"은 붙여 쓰고 "내/이내/사이"는 띄어 쓴다. 뒤에 한글이 이어지면
    # 후자는 제외 - "최근 3일 내용"의 "내용"을 "내"+"용"으로 자르지 않기 위해서.
    rf"|{RECENT_SPAN_PATTERN}(?:간|동안)?(?:\s*(?:이내|내|사이)(?![가-힣]))?"
    r"|(?:이번|지난|저번)\s?(?:주|달|분기)(?:에|의)?"
    r"|[Qq]\d|\d\s?분기"
    r"|\d{4}\s?년(?:도)?(?:에|의)?"
    r"|오늘|어제|그저께|그제|올해|금년|작년|재작년"
    r"|\bthis\s+year\b|\blast\s+year\b|\bthis\s+month\b|\blast\s+month\b"
)

# semantic_query에서 **지우면 안 되는** 마커 구절 - 마커이면서 동시에 코퍼스 본문의 내용어인
# 제품 고유명. Drive 공유요청 알림 메일 본문에는 "Google Drive"가 그대로 쓰여 있어서, 이 구절까지
# 지우면 벡터/어휘 arm이 그 문서를 못 집는다. v0에서 실측한 차이(다른 조건 동일):
# 지울 때 hit@1 0.814 / ndcg@10 0.890, 남길 때 hit@1 0.857 / ndcg@10 0.897.
# "드라이브"/"메일"/"슬랙" 같은 맨 종류어에는 이 예외를 주지 않는다 - 검색어로서 정보량이 거의
# 없다. 슬랙/노션 고유명까지 넓히는 건 아직 측정하지 않았다.
# 이 패턴도 반드시 `_latin_aware`를 거쳐 쓴다. 끝의 `\b`를 그대로 두면 "Google Drive에서"의
# `e`와 `에` 사이에 경계가 없어 keep이 실패하고, 마커 쪽만 지워져 **"Google"만 남는 파편**이
# 된다(실측: `v0-factoid-009` 1.000 -> 0.631). 지우기와 남기기가 같은 경계 규칙을 써야 한다.
_MARKER_KEEP_PATTERN = r"(?:구글|google)\s*(?:드라이브|g?drive)\b"

_THIS_YEAR_RE = re.compile(r"올해|금년|\bthis\s+year\b")
_LAST_YEAR_RE = re.compile(r"작년|\blast\s+year\b")
_YEAR_BEFORE_LAST_RE = re.compile(r"재작년")

# 따옴표 구절 - router.py의 _QUOTED_RE와 같은 표면형이지만 내용을 캡처해야 해서 별도 정의.
_QUOTED_RE = re.compile(r"[\"“”'‘’「『]([^\"“”'‘’」』]+?)[\"“”'‘’」』]")
# 파일명: 확장자가 붙은 토큰. 확장자 목록은 parsers/registry.py가 다루는 형식 + 흔한 것.
_FILENAME_RE = re.compile(
    r"(?<![\w가-힣.])([\w가-힣.\-]+\.(?:pdf|xlsx|xls|docx|doc|pptx|ppt|hwpx?|txt|md|csv|json))(?![\w가-힣])",
    re.IGNORECASE,
)
# 코드형 식별자: ERR_CONNECTION_RESET, PRJ-2025-001, KO-abc123, #1234, ABC123(대문자+숫자 혼합).
_CODE_ID_RE = re.compile(
    r"(?<![\w가-힣])(?:"
    r"[A-Z][A-Z0-9]*(?:[_-][A-Za-z0-9]+)+"  # ERR_CONNECTION_RESET, PRJ-2025-001
    r"|[A-Za-z]{2,}[_-][A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*"  # ko-abc123, jira-key
    r"|#\d+"
    r"|[A-Z]{2,}\d{2,}"  # ABC123
    r")(?![A-Za-z0-9_-])"
)
# 언더스코어/하이픈으로 연결된 혼합 토큰(한글 포함, 숫자 시작 허용) - "2025_SH_업무_보고서".
# 파일명이 아니어도 특정 문서 제목을 그대로 옮겨 적은 형태라 document_reference로 본다.
_JOINED_TOKEN_RE = re.compile(r"(?<![\w가-힣])[A-Za-z0-9가-힣]+(?:[_-][A-Za-z0-9가-힣]+)+")
# 날짜(2025-03-15)처럼 숫자와 구분자만으로 된 토큰은 식별자가 아니다.
_DIGITS_ONLY_RE = re.compile(r"^[\d_-]+$")
_TRAILING_PARTICLE_RE = re.compile(r"(?<=[가-힣A-Za-z0-9])(?:으로|에서|에게|께서|이랑|부터|까지|와|과|랑|을|를|이|가|은|는|의|에|로|도)$")

# 이메일 주소 / 도메인 - `sources.metadata.participants`가 gmail에서 ["이름 <주소>", ...] 형태라
# 주소도 도메인도 그 JSON 텍스트에 그대로 들어 있다. `search/retrieve.py::_metadata_filter_where`가
# participants에 ILIKE '%값%'을 거는 계약이라, 여기서 participants에 실어 주기만 하면 된다.
# v0-metadata_filter-001("nonghyup.com 도메인 발신자가 보낸 메일")은 도메인이 어느 필드에도 안
# 실려 필터가 이름 하나(오탐)뿐이었다.
# 뒤 경계를 `[\w-]`가 아니라 주소에 쓰이는 문자로만 막는 이유: 한국어 조사가 바로 붙는다
# ("...com이 보낸") - `\w`는 한글도 포함해서 매치가 통째로 실패한다.
_EMAIL_RE = re.compile(r"(?<![\w.@+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![A-Za-z0-9._%+-])")
# 도메인은 (a) TLD 화이트리스트, (b) 등록 가능 도메인 형태(label + TLD)로만 인정한다.
# (a) `[A-Za-z]{2,}`로 열어 두면 "보고서.내용" 같은 한글 어절 사이의 마침표나 낯선 확장자까지
#     도메인으로 오인한다. 다단 TLD(co.kr)를 먼저 시도해야 "nonghyup.co.kr"이 잘리지 않는다.
# (b) 서브도메인까지 붙은 호스트명은 v0 실측상 발신 도메인이 아니라 URL이다 -
#     `v0-exact_match-002`("git.sparklingsoda.ai:8443/..."), `v0-exact_match-003`
#     ("prepay-docs.168-107-13-173.sslip.io")는 정확 일치로 찾아야 할 문자열인데, 이걸
#     participants로 빨아들이면 identifiers/lexical이 통째로 사라진다. 앞뒤에 `/`나 `:`가
#     붙은 URL 문맥도 같은 이유로 제외.
_TLD_ALT = r"co\.kr|or\.kr|ne\.kr|go\.kr|re\.kr|com|net|org|edu|gov|biz|info|io|ai|dev|app|kr|jp|cn|us|uk|de|fr"
_DOMAIN_RE = re.compile(
    rf"(?<![\w.@+:/-])[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.(?:{_TLD_ALT})(?![A-Za-z0-9.:/-])",
    re.IGNORECASE,
)

_NAME = r"[가-힣]{2,4}|[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"
# "김철수가 (2025년 3월에) 작성한" - 이름과 동사 사이에 시간 표현 등이 끼어도 잡히도록 짧은 임의
# 구간을 허용한다.
_AUTHOR_KO_RE = re.compile(
    rf"(?P<name>{_NAME})(?:님)?(?:가|이|께서)\s+(?:[^\s]+\s+){{0,3}}?(?:작성한|작성했|작성|쓴|쓰신|보낸|보내신|올린|올리신|만든|공유한)"
)
_AUTHOR_EN_RE = re.compile(rf"\b(?:from|by|written\s+by|sent\s+by|authored\s+by)\s+(?P<name>{_NAME})")
_PARTICIPANT_KO_RE = re.compile(
    rf"(?P<name>{_NAME})(?:님)?(?:와|과|이랑|랑|하고)(?:의|\s)\s*(?:회의|미팅|대화|논의|통화|스레드|주고받은|나눈)"
)
_PARTICIPANT_EN_RE = re.compile(rf"\b(?:with|between)\s+(?P<name>{_NAME})")
# "A 프로젝트"/"OKF프로젝트" - 앞말이 관형어/시간어면 프로젝트명이 아니다.
_PROJECT_RE = re.compile(r"(?<![\w가-힣])(?P<name>[A-Za-z0-9가-힣]{1,20})\s?프로젝트")
# 이름 자리에 올 수 있지만 사람 이름이 아닌 일반명사 - "담당자가 작성한"은 작성자 필터가 아니라
# multi-hop("담당자가 누구인지 먼저 찾아야 함") 신호다.
_NAME_STOPWORDS = frozenset(
    {
        "담당자", "담당", "누군가", "누가", "사람", "팀", "팀원", "개발자", "고객", "상대방", "회사",
        "부서", "본인", "저희", "우리",
        # 메일/메시지의 역할 명사. "발신자가 보낸 메일"(v0-metadata_filter-001)에서
        # authors=["발신자"]가 실려 ILIKE '%발신자%'가 0행 → 필터가 통째로 무시됐다
        # (applied="filter_empty_ignored"). 역할이지 사람을 특정하는 이름이 아니다.
        "발신자", "수신자", "참조", "보낸사람", "보낸이", "받는사람", "받는이", "발신인", "수신인",
        "작성자", "관리자", "책임자", "참석자", "참여자",
    }
)
_PROJECT_STOPWORDS = frozenset(
    {"올해", "작년", "이번", "지난", "그", "이", "저", "그런", "이런", "모든", "전체", "관련", "새", "신규", "각", "해당", "어떤", "무슨"}
)
_COMPARISON_SPLIT_RE = re.compile(r"(?<=[가-힣A-Za-z0-9\)])(?:와|과|이랑|랑|하고)\s+|\s+(?:vs\.?|versus|and|및)\s+|,\s*")
# 비교 대상 분리용 꼬리 마커 - "vs/versus"는 대상 사이의 접속어라 꼬리가 아니므로 제외한다.
_COMPARISON_CONNECTIVE_MARKERS = frozenset({r"\bvs\.?\b", r"\bversus\b"})
_COMPARISON_TAIL_RE = re.compile(
    r"(?:\s*(?:의|은|는|이|가|을|를|에서|에|간|사이))?\s*(?:"
    + "|".join(_latin_aware(m) for m in COMPARISON_MARKERS if m not in _COMPARISON_CONNECTIVE_MARKERS)
    + r")"
)
# semantic_query 정리용 - 검색어로서 정보량이 없는 지시/보조 표현.
_FILLER_RE = re.compile(
    # "발신자"/"도메인" 같은 역할·메타 명사는 `_NAME_STOPWORDS`가 작성자 오탐은 막아 주지만
    # (v0-metadata_filter-001) semantic_query에는 그대로 남아 벡터 검색어를 흐린다.
    r"발신자|수신자|발신인|수신인|보낸\s*사람|받는\s*사람|도메인"
    r"|관련(?:된|해서|하여|한)?|문서(?:들)?|자료(?:들)?|파일(?:들)?|알려\s*줘|보여\s*줘|찾아\s*줘|해\s*줘|해줄래|해주세요|주세요|해\s*봐"
    r"|뭐야|뭐였(?:지|어)|무엇(?:이야|인가요?)?|있(?:어|나요|었어|었나요)|좀|해서|대해(?:서)?|대한|에\s*대해"
    r"|\bplease\b|\bshow\s+me\b|\btell\s+me\b|\bfind\b|\bsearch\b|\bdocuments?\b|\babout\b|\brelated\b"
)
_DANGLING_PARTICLE_RE = re.compile(r"(?:^|\s)(?:은|는|이|가|을|를|의|에|에서|로|으로|와|과|도|중)(?=\s|$)")


@dataclass
class QueryConstraints:
    """한 질의에서 뽑아낸 구조화된 제약 + 의도 플래그. 리스트 필드는 비어 있을 수 있고 None이
    아니다 - 소비자(`search/retrieve.py`)가 `if constraints.authors:`로 바로 분기할 수 있게."""

    query: str
    time_range: TimeRange | None = None
    authors: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    # 파일명 확장자(".xlsx") / MIME 타입("text/markdown"). 둘 다 구조적 술어다 -
    # `source_types`처럼 표면 어휘로 찍은 추측이 아니라 질의가 명시한 조건이다.
    file_extensions: list[str] = field(default_factory=list)
    mime_types: list[str] = field(default_factory=list)
    document_types: list[str] = field(default_factory=list)
    project_hints: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    quoted_phrases: list[str] = field(default_factory=list)
    document_reference: str | None = None
    wants_whole_document: bool = False
    wants_summary: bool = False
    wants_latest: bool = False
    wants_exhaustive: bool = False
    wants_count: bool = False
    is_negative: bool = False
    is_comparison: bool = False
    is_multihop: bool = False
    comparison_targets: list[str] = field(default_factory=list)
    semantic_query: str = ""
    # 필드명 → "rule"|"llm". 어떤 값이 어느 단계에서 왔는지 - "LLM 보강이 실제로
    # 무엇을 바꿨나"를 집계하고, 디버깅 때 규칙 오탐인지 LLM 환각인지 가르기 위한 것.
    origins: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.time_range is not None:
            data["time_range"] = {
                "start": self.time_range.start.isoformat() if self.time_range.start else None,
                "end": self.time_range.end.isoformat() if self.time_range.end else None,
                "label": self.time_range.label,
            }
        return data

    def _mark(self, field_name: str, origin: str) -> None:
        self.origins[field_name] = origin


# --- 규칙 기반 1단계 ------------------------------------------------------------


def _any_marker(markers: tuple[str, ...], text: str) -> bool:
    return any(re.search(_latin_aware(pattern), text, re.IGNORECASE) for pattern in markers)


def _strip_source_type_markers(text: str) -> str:
    """semantic_query에서 source_type 마커 구절을 걷어낸다 - 단 `_MARKER_KEEP_PATTERN`에 걸리는
    구절은 그대로 둔다.

    keep 쪽을 alternation **앞**에 두고, 그쪽이 매치되면 원문을 되돌려주는 방식이다. 파이썬 `|`는
    leftmost-first라 keep이 그 구간을 먼저 먹고 뒤의 마커 패턴은 그 안을 다시 물지 못한다 -
    자리표시자로 마스킹했다가 되돌리는 것보다 짧고, 되돌리기 전에 자리표시자가 다른 정리 단계에
    씹힐 위험도 없다."""

    def _keep_or_blank(m: re.Match[str]) -> str:
        return m.group("keep") or " "

    for pattern, _label in SOURCE_TYPE_MARKERS:
        text = re.sub(
            rf"(?P<keep>{_latin_aware(_MARKER_KEEP_PATTERN)})|(?:{_latin_aware(pattern)})",
            _keep_or_blank,
            text,
            flags=re.IGNORECASE,
        )
    return text


def _strip_markers(markers: tuple[str, ...], text: str) -> str:
    for pattern in markers:
        text = re.sub(_latin_aware(pattern), " ", text, flags=re.IGNORECASE)
    return text


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _strip_particle(token: str) -> str:
    """'2025_SH_업무_보고서를' → '2025_SH_업무_보고서'. 조사 자체가 토큰의 마지막 구성요소
    전부인 경우(예: '_를')는 건드리지 않는다."""
    stripped = _TRAILING_PARTICLE_RE.sub("", token)
    return stripped if stripped and not stripped.endswith(("_", "-")) else token


def _supplemental_time_range(text: str, now: datetime) -> TimeRange | None:
    """temporal.py에 없는 '올해/작년/재작년'만 보충 - 다른 표현은 전부 temporal.py 담당."""

    def _year(year: int, label: str) -> TimeRange:
        start = now.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return TimeRange(start, start.replace(year=year + 1), label)

    if _YEAR_BEFORE_LAST_RE.search(text):
        return _year(now.year - 2, "재작년")
    if _LAST_YEAR_RE.search(text):
        return _year(now.year - 1, "작년")
    if _THIS_YEAR_RE.search(text):
        return _year(now.year, "올해")
    return None


def _resolve_time_range(text: str, now: datetime) -> TimeRange | None:
    return parse_temporal_expression(text, now=now) or _supplemental_time_range(text, now)


def _extract_comparison_targets(text: str) -> list[str]:
    """'2024년과 2025년 사업계획의 차이' → ['2024년 사업계획', '2025년 사업계획'].
    비교 마커 앞부분을 접속 조사/vs/쉼표로 쪼개 마지막 두 조각을 취한다. 앞 조각이 시간 표현만으로
    이뤄져 있고 뒤 조각에 공통 머리말(사업계획)이 있으면 앞 조각에도 붙여 준다 - 한국어에서
    'A년과 B년 X'는 'A년 X와 B년 X'의 생략형이라서."""
    m = _COMPARISON_TAIL_RE.search(text)
    # "Redis vs Memcached"처럼 꼬리 마커 없이 접속어만 있는 형태는 전체가 머리말이다.
    head = text[: m.start()].strip() if m else text.strip()
    parts = [p.strip() for p in _COMPARISON_SPLIT_RE.split(head) if p and p.strip()]
    if len(parts) < 2:
        return []
    left, right = parts[-2], parts[-1]
    left = _DANGLING_PARTICLE_RE.sub("", _strip_particle(left)).strip()
    right = _DANGLING_PARTICLE_RE.sub("", _strip_particle(right)).strip()
    left_rest = _TEMPORAL_SPAN_RE.sub("", left).strip()
    right_rest = _TEMPORAL_SPAN_RE.sub("", right).strip()
    if not left_rest and right_rest and _TEMPORAL_SPAN_RE.search(right):
        left = f"{left} {right_rest}"
    return _dedupe([left, right])


def _clean_semantic_query(text: str) -> str:
    text = _FILLER_RE.sub(" ", text)
    text = re.sub(r"[?？!！.,;:()\[\]{}]", " ", text)
    text = _DANGLING_PARTICLE_RE.sub(" ", text)
    # "사업계획의" → "사업계획", "프로젝트에서" → "프로젝트". 3음절 이상 한글 어절에만 - "회의"/
    # "차이"처럼 조사로 끝나 보이는 2음절 명사를 잘라먹지 않도록.
    tokens = [
        _strip_particle(tok) if re.fullmatch(r"[가-힣]{3,}", tok) else tok for tok in text.split()
    ]
    return " ".join(tokens).strip()


def extract_constraints_rules(query: str, *, now: datetime | None = None) -> QueryConstraints:
    """LLM 없이 결정론적으로 제약을 뽑는다. `now`는 상대 시간 표현("올해", "지난달")의 기준 -
    테스트/평가에서는 고정값을 주입해 재현성을 확보한다."""
    now = now or datetime.now(timezone.utc)
    c = QueryConstraints(query=query)
    working = query.strip()

    # 1) 따옴표 구절 - 가장 강한 정확 일치 신호. 먼저 떼어내야 안의 토큰이 식별자로 이중 추출되지 않는다.
    c.quoted_phrases = _dedupe(_QUOTED_RE.findall(working))
    working = _QUOTED_RE.sub(" ", working)

    # 2) 이메일 주소 / 도메인 - 식별자 추출보다 먼저 걷어내야 한다. "hong_gil@nonghyup.com"의
    # 로컬 파트는 `_CODE_ID_RE`가 코드형 식별자로 오인해 엉뚱한 document_lookup을 켠다.
    contact_handles = _dedupe(_EMAIL_RE.findall(working))
    working = _EMAIL_RE.sub(" ", working)
    contact_handles += _dedupe(_DOMAIN_RE.findall(working))
    working = _DOMAIN_RE.sub(" ", working)

    # 3) 파일명 / 코드형 식별자 / 연결 토큰(문서 참조)
    filenames = _dedupe(_FILENAME_RE.findall(working))
    working = _FILENAME_RE.sub(" ", working)
    code_ids = _dedupe([_strip_particle(t) for t in _CODE_ID_RE.findall(working)])
    working = _CODE_ID_RE.sub(" ", working)
    joined_refs: list[str] = []
    for raw in _JOINED_TOKEN_RE.findall(working):
        token = _strip_particle(raw)
        if _DIGITS_ONLY_RE.match(token):
            continue
        joined_refs.append(token)
    working = _JOINED_TOKEN_RE.sub(" ", working)
    c.identifiers = _dedupe(code_ids + filenames)
    references = _dedupe(filenames + joined_refs)
    if references:
        c.document_reference = references[0]
    if len(references) > 1:
        # 두 번째 이후 문서 참조는 잃지 않도록 identifiers에 남긴다(비교 질의 등).
        c.identifiers = _dedupe(c.identifiers + references[1:])

    # 따옴표/식별자만 걷어낸 상태 - 의도 마커는 이 텍스트에서 본다(작성자 구절을 지운 뒤에 보면
    # "담당자가 작성한" 같은 multi-hop 신호가 같이 사라진다).
    marker_text = working

    # 4) 시간 범위 - 원문 기준(작성자 구절 안의 "2025년 3월"도 잡아야 하므로 working이 아니라 query)
    c.time_range = _resolve_time_range(query, now)

    # 5) 작성자 / 참여자 / 프로젝트 힌트
    for regex in (_AUTHOR_KO_RE, _AUTHOR_EN_RE):
        for m in regex.finditer(working):
            if m.group("name") in _NAME_STOPWORDS:
                continue
            c.authors.append(m.group("name"))
            working = working.replace(m.group(0), " ", 1)
    c.authors = _dedupe(c.authors)
    for regex in (_PARTICIPANT_KO_RE, _PARTICIPANT_EN_RE):
        for m in regex.finditer(working):
            if m.group("name") not in _NAME_STOPWORDS:
                c.participants.append(m.group("name"))
    # 주소/도메인이 앞 - 이름보다 특정력이 강한 신호다(participants는 AND라 순서 자체는 무관).
    c.participants = _dedupe(contact_handles + c.participants)
    for m in _PROJECT_RE.finditer(working):
        name = m.group("name")
        if name not in _PROJECT_STOPWORDS and not _TEMPORAL_SPAN_RE.fullmatch(name):
            c.project_hints.append(name)
    c.project_hints = _dedupe(c.project_hints)
    c.entities = _dedupe(c.authors + c.participants + c.project_hints)

    # 6) 소스 / 문서 타입 힌트
    c.source_types = _dedupe(
        [label for pattern, label in SOURCE_TYPE_MARKERS if re.search(_latin_aware(pattern), working, re.IGNORECASE)]
    )
    c.document_types = _dedupe(
        [label for pattern, label in DOCUMENT_TYPE_MARKERS if re.search(_latin_aware(pattern), working, re.IGNORECASE)]
    )
    c.file_extensions = _dedupe(
        [
            ext
            for pattern, exts in FILE_EXTENSION_MARKERS
            if re.search(_latin_aware(pattern), working, re.IGNORECASE)
            for ext in exts
        ]
    )
    c.mime_types = _dedupe([m.group(1).lower() for m in _MIME_TYPE_RE.finditer(query)])

    # 7) 의도 플래그
    c.wants_latest = _any_marker(LATEST_MARKERS, marker_text)
    c.wants_exhaustive = _any_marker(EXHAUSTIVE_MARKERS, marker_text)
    c.wants_count = _any_marker(COUNT_MARKERS, marker_text)
    c.wants_summary = _any_marker(SUMMARY_MARKERS, marker_text)
    c.is_negative = _any_marker(NEGATIVE_MARKERS, marker_text)
    c.is_comparison = _any_marker(COMPARISON_MARKERS, marker_text)
    c.is_multihop = _any_marker(MULTIHOP_MARKERS, marker_text)
    # "특정 문서 + 요약" = Whole-document Summary 유형: 청크 몇 개로는 못 하고 문서 전체를 읽어야 한다.
    c.wants_whole_document = _any_marker(WHOLE_DOCUMENT_MARKERS, marker_text) or (
        c.wants_summary and c.document_reference is not None
    )

    if c.is_comparison:
        c.comparison_targets = _extract_comparison_targets(marker_text)
        # 비교 대상마다 다른 시간 범위를 가지면(2024년 vs 2025년) 단일 time_range 필터는 한쪽을
        # 잘라내므로 비워 둔다 - 시간 조건은 각 comparison_target이 들고 간다.
        target_ranges = [_resolve_time_range(t, now) for t in c.comparison_targets]
        labels = {r.label for r in target_ranges if r is not None}
        if len(labels) > 1:
            c.time_range = None

    # 8) semantic_query - 제약 표현을 걷어낸 나머지
    semantic = _TEMPORAL_SPAN_RE.sub(" ", working)
    semantic = _strip_source_type_markers(semantic)
    for markers in (
        LATEST_MARKERS,
        EXHAUSTIVE_MARKERS,
        COUNT_MARKERS,
        SUMMARY_MARKERS,
        WHOLE_DOCUMENT_MARKERS,
        NEGATIVE_MARKERS,
        COMPARISON_MARKERS,
    ):
        semantic = _strip_markers(markers, semantic)
    c.semantic_query = _clean_semantic_query(semantic)

    for name, value in asdict(c).items():
        if name in {"query", "origins"}:
            continue
        if value not in (None, False, "", []):
            c._mark(name, ORIGIN_RULE)
    return c


# --- LLM 보강 2단계 -------------------------------------------------------------

# LLM이 손댈 수 있는 필드 - 이 밖의 필드(time_range/identifiers/quoted_phrases/의도 플래그)는
# 응답에 있어도 무시한다.
LLM_ENRICHABLE_FIELDS: tuple[str, ...] = (
    "entities",
    "authors",
    "document_reference",
    "document_types",
    "semantic_query",
    "is_multihop",
    "comparison_targets",
)

_ENRICH_SYSTEM_PROMPT = """당신은 검색 질의 분석기다. 사용자의 질의와 규칙 기반으로 이미 추출된 제약을 받아,
규칙이 놓친 것만 보강한다. 지식 베이스는 사내 문서(보고서/회의록/계획서/메일/슬랙)이다.

보강할 필드(이것만 출력):
- entities: 사람/회사/프로젝트/제품명 등 고유명사 목록 (규칙 결과에 추가할 것만이 아니라 전체 목록)
- authors: 작성자/발신자 이름 목록
- document_reference: 질의가 특정 문서 하나를 가리키면 그 문서 제목 구절, 아니면 null
- document_types: 문서 종류 라벨 목록. 허용 값: {document_type_labels}
- semantic_query: 시간 조건/작성자/식별자/지시어("요약해줘" 등)를 걷어낸, 벡터 검색용 핵심 검색 문장
- is_multihop: 앞 검색 결과를 알아야 다음 검색이 가능한 질의면 true
- comparison_targets: 비교 질의면 비교 대상 2개 이상, 아니면 빈 배열

규칙:
- 시간 표현, 식별자(에러코드/파일명), 의도 플래그는 이미 확정됐다. 절대 다시 해석하거나 언급하지 말 것.
- 질의에 근거 없는 값을 지어내지 말 것. 확신이 없으면 규칙 결과를 그대로 둘 것.
- 다른 설명 없이 아래 JSON 객체 하나만 출력할 것(마크다운 코드펜스 없이):
{{"entities": [], "authors": [], "document_reference": null, "document_types": [], "semantic_query": "", "is_multihop": false, "comparison_targets": []}}"""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """코드펜스/앞뒤 설명문이 붙어 있어도 첫 '{'부터 마지막 '}'까지를 JSON으로 시도한다.
    planner.py의 `_strip_code_fence`와 같은 목적이지만 planner를 import하면 service/storage까지
    끌려오므로 여기서 따로 둔다."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _dedupe([v for v in value if isinstance(v, str)])


def merge_llm_enrichment(base: QueryConstraints, payload: dict[str, Any]) -> QueryConstraints:
    """규칙 결과 `base` 위에 LLM 응답을 병합한다(in place, 반환값은 같은 객체).

    병합 정책 - "LLM은 보강만, 규칙이 확정한 것은 못 뒤집는다":
    - entities/authors/document_types: 합집합(규칙 값 우선 순서 유지). 삭제 불가.
    - document_reference: 규칙이 못 찾았을 때만 채움(규칙의 토큰 기반 참조가 더 정확하다).
    - semantic_query: 비어 있지 않은 문자열이면 교체 - 재작성이 이 필드의 존재 이유.
    - is_multihop: OR. 규칙이 켠 걸 LLM이 끌 수 없다.
    - comparison_targets: 규칙이 is_comparison을 켠 경우에만, 2개 이상이면 교체(규칙의 조사 분리는
      거칠어서 LLM 쪽이 보통 낫다). is_comparison 자체는 규칙 소관이라 LLM이 켤 수 없다.
    - 그 외 필드는 응답에 있어도 무시.
    """
    allowed_document_types = {label for _pattern, label in DOCUMENT_TYPE_MARKERS}

    entities = _str_list(payload.get("entities"))
    if entities and entities != base.entities:
        base.entities = _dedupe(base.entities + entities)
        base._mark("entities", ORIGIN_LLM)

    authors = _str_list(payload.get("authors"))
    if authors and _dedupe(base.authors + authors) != base.authors:
        base.authors = _dedupe(base.authors + authors)
        base.entities = _dedupe(base.entities + authors)
        base._mark("authors", ORIGIN_LLM)

    document_types = [d for d in _str_list(payload.get("document_types")) if d in allowed_document_types]
    if document_types and _dedupe(base.document_types + document_types) != base.document_types:
        base.document_types = _dedupe(base.document_types + document_types)
        base._mark("document_types", ORIGIN_LLM)

    reference = payload.get("document_reference")
    if base.document_reference is None and isinstance(reference, str) and reference.strip():
        base.document_reference = reference.strip()
        base._mark("document_reference", ORIGIN_LLM)
        if base.wants_summary:
            base.wants_whole_document = True

    semantic = payload.get("semantic_query")
    if isinstance(semantic, str) and semantic.strip() and semantic.strip() != base.semantic_query:
        base.semantic_query = semantic.strip()
        base._mark("semantic_query", ORIGIN_LLM)

    if payload.get("is_multihop") is True and not base.is_multihop:
        base.is_multihop = True
        base._mark("is_multihop", ORIGIN_LLM)

    targets = _str_list(payload.get("comparison_targets"))
    if base.is_comparison and len(targets) >= 2 and targets != base.comparison_targets:
        base.comparison_targets = targets
        base._mark("comparison_targets", ORIGIN_LLM)

    return base


def _enrichment_user_message(constraints: QueryConstraints) -> str:
    rule_view = {name: constraints.to_dict()[name] for name in LLM_ENRICHABLE_FIELDS}
    rule_view["time_range_label"] = constraints.time_range.label if constraints.time_range else None
    rule_view["identifiers"] = constraints.identifiers
    return f"질의: {constraints.query}\n\n규칙 기반 추출 결과(JSON):\n{json.dumps(rule_view, ensure_ascii=False)}"


async def extract_constraints(
    query: str, provider: ChatProvider | None = None, *, now: datetime | None = None
) -> QueryConstraints:
    """규칙 1단계 + (provider가 있으면) LLM 보강 2단계. provider가 None이면 규칙 결과 그대로.

    `ChatProvider.send()`는 temperature를 노출하지 않는다(벤더 중립 추상화가 파라미터를 최소로
    유지하기 때문). 결정성은 "JSON 하나만 출력" 프롬프트와 위 병합 정책(규칙 확정 필드 불변)으로
    확보하고, 실제 temperature=0은 provider 구성(KF_* 설정) 쪽 책임으로 둔다."""
    constraints = extract_constraints_rules(query, now=now)
    if provider is None:
        return constraints

    system = _ENRICH_SYSTEM_PROMPT.format(document_type_labels=", ".join(label for _p, label in DOCUMENT_TYPE_MARKERS))
    try:
        turn = await provider.send([Message(role="user", content=_enrichment_user_message(constraints))], [], system)
        payload = _extract_json_object(turn.text or "")
    except Exception as exc:  # noqa: BLE001 - 보강 실패가 검색을 막으면 안 된다, 규칙 결과로 degrade
        logger.warning("constraint enrichment failed, keeping rule-based constraints: %s", exc)
        return constraints
    if payload is None:
        logger.warning("constraint enrichment returned non-JSON, keeping rule-based constraints")
        return constraints
    return merge_llm_enrichment(constraints, payload)


# --- 키워드 term 분해 -----------------------------------------------------------

# 어떤 문서에나 있어서 랭킹을 가르지 못하는 일반 명사. term 하나가 후보 리스트 하나를 만드는
# 구조(`search/retrieve.py::_term_fallback`)라, 이런 term은 신호 없는 리스트를 늘려 max_terms
# 예산만 잡아먹는다. `_FILLER_RE`(질의 전체에서 지우는 군더더기)와 달리 여기서만 뺀다 -
# semantic_query 자체에는 남아 있어야 벡터 검색이 문맥으로 쓴다.
_TERM_NOISE = frozenset(
    {"내용", "관련", "정보", "자료", "부분", "목록", "리스트", "상황", "경우", "이야기", "순서", "여부"}
)
_TERM_SPLIT_RE = re.compile(r"[^0-9A-Za-z가-힣_.\-]+")


def keyword_terms(constraints: QueryConstraints, *, max_terms: int = 10) -> list[str]:
    """질의를 개별 검색어(term)로 쪼갠다 - `search/retrieve.py`가 임베딩 없이 돌 때
    `lexical_search`를 term마다 한 번씩 부르기 위한 입력.

    왜 필요한가: `lexical_search`/`document_search`는 pg_trgm 위의 (전체 질의 substring →
    모든 토큰 substring → `word_similarity`) 3단 매칭이다. "농협 MVP 투입 인력 명함 제작 논의"
    같은 자연어 질의는 세 단계 모두 실패한다 - 그 문자열을 통째로 담은 청크도, 일곱 토큰을
    전부 담은 청크도 없고, 질의가 길어 `word_similarity`도 임계값 아래로 떨어진다. 그래서
    v0 baseline에서 conversation_thread/sequence 질의가 히트 0으로 끝났다. term 단위로 나눠
    각각 조회하면 RRF가 "여러 term이 겹친 문서"를 위로 올려 준다.

    순서는 정밀도 순이다: `identifiers`/`quoted_phrases`(리터럴) → `entities` →
    `semantic_query` 토큰. `max_terms`로 자를 때 앞쪽이 살아남게 하기 위한 것."""
    raw: list[str] = [
        *constraints.identifiers,
        *constraints.quoted_phrases,
        *constraints.entities,
        *_TERM_SPLIT_RE.split(constraints.semantic_query),
    ]
    terms: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip()
        # `_clean_semantic_query`와 같은 이유로 3음절 이상 한글에만 조사를 뗀다 - "논의"의 "의",
        # "묻는"의 "는"을 조사로 보면 1글자만 남아 term이 통째로 사라진다.
        term = _strip_particle(token) if re.fullmatch(r"[가-힣]{3,}", token) else token
        # 1글자 term은 거의 모든 청크에 걸려 랭킹 신호가 없다.
        if len(term) < 2 or term in _TERM_NOISE:
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms[:max_terms]


# --- 전략 힌트 -----------------------------------------------------------------


def suggest_strategies(constraints: QueryConstraints) -> list[str]:
    """제약에서 검색 전략 이름 목록을 결정론적으로 도출한다. 이 이름들은 `search/retrieve.py`와의
    계약이다. 앞쪽은 **후보 생성** 전략(무엇을 어디서 찾을지), 뒤쪽은 그 결과에 적용하는
    **후처리/집계** 전략이며, 각 그룹 안에서는 우선순위(정확한 신호 → 느슨한 신호) 순이다.

    후보 생성:
    - "document_lookup": `document_reference`/`identifiers`로 문서 제목·파일명을 찾는다
      (Navigational/Specific Document QA). 결과는 문서 단위, 이후 whole_document/vector가 그 안을 본다.
    - "lexical": `identifiers`/`quoted_phrases`를 본문에 정확 일치(substring/BM25)로 찾는다(Exact Match).
      임베딩은 에러코드 같은 토큰에 의미가 없어 못 잡는다.
    - "metadata_filter": `authors`/`participants`/`source_types`/`document_types`/`time_range`를
      sources.metadata/타임스탬프 WHERE 조건으로 건다(Metadata Filter/Temporal). 다른 후보 생성 전략과
      결합되는 필터이지 단독 랭킹이 아니다 - 단독이면 필터 결과를 최신순 정렬로 돌려준다.
    - "vector": `semantic_query`로 임베딩 유사도 검색(Entity-centric/Ambiguous/일반 QA).
    - "keyword_arm": 같은 `semantic_query`를 제목(`document_search`)·본문(`lexical_search`)에도 걸어
      벡터와 나란히 RRF에 넣는 어휘 arm. `vector`와 늘 짝으로 나온다(하이브리드 검색의 반대쪽 팔) -
      따로 세워 둔 이유는 A/B에서 한쪽만 끄기 위해서다(`retrieve.py::_Run.keyword_arm`).

    후처리/집계:
    - "latest": 후보를 소스 타임스탬프 내림차순 정렬 후 상위 1~N만(Latest/Version).
    - "exhaustive": 유사도 top-k 절단 대신 조건에 맞는 전부를 페이지네이션으로 수집(Set Query).
    - "count": 후보 개수를 답으로 - 필터 조건에서 SQL COUNT로 직행 가능(Aggregation).
    - "whole_document": 청크가 아니라 문서 본문 전체를 `read_document`로 읽어 답한다(Whole-document Summary).
    - "comparison": `comparison_targets`별로 독립 검색 후 나란히 놓는다(Cross-document Comparison).
    - "negative": 대상 집합(보통 project/document_reference 범위)을 exhaustive로 모은 뒤 부재를
      판정한다 - 유사도 검색은 "없음"을 증명할 수 없다(Negative).
    - "multihop": `search/planner.py`로 분해 실행(Multi-hop).
    """
    strategies: list[str] = []
    if constraints.document_reference or constraints.identifiers:
        strategies.append(STRATEGY_DOCUMENT_LOOKUP)
    if constraints.identifiers or constraints.quoted_phrases:
        strategies.append(STRATEGY_LEXICAL)
    if (
        constraints.authors
        or constraints.participants
        or constraints.source_types
        or constraints.document_types
        or constraints.file_extensions
        or constraints.mime_types
        or constraints.time_range is not None
    ):
        strategies.append(STRATEGY_METADATA_FILTER)
    if constraints.semantic_query:
        # 둘은 하이브리드 한 쌍이라 라우팅에서는 늘 함께 나간다 - 분리는 override 전용이다.
        strategies.append(STRATEGY_VECTOR)
        strategies.append(STRATEGY_KEYWORD_ARM)

    if constraints.wants_latest:
        strategies.append(STRATEGY_LATEST)
    if constraints.wants_exhaustive or constraints.is_negative:
        strategies.append(STRATEGY_EXHAUSTIVE)
    if constraints.wants_count:
        strategies.append(STRATEGY_COUNT)
    if constraints.wants_whole_document:
        strategies.append(STRATEGY_WHOLE_DOCUMENT)
    if constraints.is_comparison:
        strategies.append(STRATEGY_COMPARISON)
    if constraints.is_negative:
        strategies.append(STRATEGY_NEGATIVE)
    if constraints.is_multihop:
        strategies.append(STRATEGY_MULTIHOP)
    return strategies
