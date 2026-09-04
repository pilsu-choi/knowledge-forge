"""Query Understanding → 구조화된 제약(QueryConstraints) 추출 - wiki/search-architecture.md
"① Query Understanding"의 명시적 레이어, wiki/query-types.md의 검색 유형 표를 실제 코드로
잡아내는 곳.

지금까지 "이 질의가 특정 문서를 가리키는가 / 작성자·기간 필터인가 / 개수를 묻는가 / 비교인가"
같은 판단은 전부 에이전트(LLM)가 매 턴 암묵적으로 했다. 그걸 굳이 결정론적인 별도 단계로 뽑아낸
이유는 두 가지다. (a) 에이전트 turn 없이 검색을 호출하는 소비자가 생겼다 - 골든셋 기반 IR 평가
하네스(`eval-ir`, wiki/search-evaluation.md)는 질의 → 검색 결과를 LLM 판단 없이, 같은 입력에
같은 출력으로 재현 가능하게 돌려야 지표가 의미를 가진다. (b) 검색 유형 표(Navigational/Temporal/
Aggregation/Exhaustive/Exact/Negative/Comparison/...)의 절반 이상은 "유사도 검색으로는 원리적으로
못 잡는" 유형이라, 검색 전략 선택 자체가 질의를 제약+의도 플래그로 먼저 분해하는 데 달려 있다.

2단계로 나눈 이유:
- 1단계 `extract_constraints_rules()`: LLM 없이 regex/사전만으로. 시간 범위(`search/temporal.py`
  재사용), 따옴표/식별자/파일명, 최신/전부/개수/요약/전체/부정/비교 마커, 작성자 조사 패턴,
  소스·문서 타입 힌트. 이 결과는 결정론적이라 평가 하네스의 기준선이 되고, 단위 테스트로 못
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

이 모듈은 `search/service.py`/`planner.py`를 import하지 않는다 - 평가 하네스가 DB/임베딩 없이
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
from typing import Any

from knowledge_forge.chat_provider import ChatProvider, Message
from knowledge_forge.search.temporal import TimeRange, parse_temporal_expression

logger = logging.getLogger(__name__)

ORIGIN_RULE = "rule"
ORIGIN_LLM = "llm"

# --- 전략 이름 (search/retrieve.py 계약) -------------------------------------

STRATEGY_DOCUMENT_LOOKUP = "document_lookup"
STRATEGY_LEXICAL = "lexical"
STRATEGY_METADATA_FILTER = "metadata_filter"
STRATEGY_VECTOR = "vector"
STRATEGY_LATEST = "latest"
STRATEGY_EXHAUSTIVE = "exhaustive"
STRATEGY_COUNT = "count"
STRATEGY_WHOLE_DOCUMENT = "whole_document"
STRATEGY_COMPARISON = "comparison"
STRATEGY_NEGATIVE = "negative"
STRATEGY_MULTIHOP = "multihop"

# --- 마커 사전 (모듈 상수 - 표면형 추가는 여기만 고치면 된다) -------------------
# 각 항목은 regex 문자열. 한국어는 어절 경계가 불규칙해서 \b를 쓰지 않고, 조사/어미가 붙어도
# 잡히도록 어간만 적는다. 영어는 \b로 단어 경계를 건다.

LATEST_MARKERS: tuple[str, ...] = (
    r"가장\s*최근",
    r"제일\s*최근",
    # "최근 3개월"은 temporal(범위)이지 latest(정렬)가 아니므로 숫자가 뒤따르는 경우는 제외
    r"최근(?!\s*\d)",
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
    r"몇\s*(?:개|건|명|번|회|종류)",
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
SOURCE_TYPE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"슬랙|\bslack\b|채널|스레드|\bDM\b", "slack"),
    (r"이메일|메일|\bg?mail\b|\be-?mail\b|수신함", "gmail"),
    (r"드라이브|\bg?drive\b|구글\s*(?:독스|문서|시트)|스프레드시트", "google_drive"),
    (r"노션|\bnotion\b", "notion"),
    (r"로컬\s*파일|\blocal\s+file", "local_file"),
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
    r"|최근\s*\d+\s*(?:일|주|개?월)(?:간|동안)?"
    r"|(?:이번|지난|저번)\s?(?:주|달|분기)(?:에|의)?"
    r"|[Qq]\d|\d\s?분기"
    r"|\d{4}\s?년(?:도)?(?:에|의)?"
    r"|오늘|어제|그저께|그제|올해|금년|작년|재작년"
    r"|\bthis\s+year\b|\blast\s+year\b|\bthis\s+month\b|\blast\s+month\b"
)
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
    {"담당자", "담당", "누군가", "누가", "사람", "팀", "팀원", "개발자", "고객", "상대방", "회사", "부서", "본인", "저희", "우리"}
)
_PROJECT_STOPWORDS = frozenset(
    {"올해", "작년", "이번", "지난", "그", "이", "저", "그런", "이런", "모든", "전체", "관련", "새", "신규", "각", "해당", "어떤", "무슨"}
)
_COMPARISON_SPLIT_RE = re.compile(r"(?<=[가-힣A-Za-z0-9\)])(?:와|과|이랑|랑|하고)\s+|\s+(?:vs\.?|versus|and|및)\s+|,\s*")
# 비교 대상 분리용 꼬리 마커 - "vs/versus"는 대상 사이의 접속어라 꼬리가 아니므로 제외한다.
_COMPARISON_CONNECTIVE_MARKERS = frozenset({r"\bvs\.?\b", r"\bversus\b"})
_COMPARISON_TAIL_RE = re.compile(
    r"(?:\s*(?:의|은|는|이|가|을|를|에서|에|간|사이))?\s*(?:"
    + "|".join(m for m in COMPARISON_MARKERS if m not in _COMPARISON_CONNECTIVE_MARKERS)
    + r")"
)
# semantic_query 정리용 - 검색어로서 정보량이 없는 지시/보조 표현.
_FILLER_RE = re.compile(
    r"관련(?:된|해서|하여|한)?|문서(?:들)?|자료(?:들)?|파일(?:들)?|알려\s*줘|보여\s*줘|찾아\s*줘|해\s*줘|해줄래|해주세요|주세요|해\s*봐"
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
    # 필드명 → "rule"|"llm". 어떤 값이 어느 단계에서 왔는지 - 평가 하네스가 "LLM 보강이 실제로
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
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in markers)


def _strip_markers(markers: tuple[str, ...], text: str) -> str:
    for pattern in markers:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
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

    # 2) 파일명 / 코드형 식별자 / 연결 토큰(문서 참조)
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

    # 3) 시간 범위 - 원문 기준(작성자 구절 안의 "2025년 3월"도 잡아야 하므로 working이 아니라 query)
    c.time_range = _resolve_time_range(query, now)

    # 4) 작성자 / 참여자 / 프로젝트 힌트
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
    c.participants = _dedupe(c.participants)
    for m in _PROJECT_RE.finditer(working):
        name = m.group("name")
        if name not in _PROJECT_STOPWORDS and not _TEMPORAL_SPAN_RE.fullmatch(name):
            c.project_hints.append(name)
    c.project_hints = _dedupe(c.project_hints)
    c.entities = _dedupe(c.authors + c.participants + c.project_hints)

    # 5) 소스 / 문서 타입 힌트
    c.source_types = _dedupe([label for pattern, label in SOURCE_TYPE_MARKERS if re.search(pattern, working, re.IGNORECASE)])
    c.document_types = _dedupe(
        [label for pattern, label in DOCUMENT_TYPE_MARKERS if re.search(pattern, working, re.IGNORECASE)]
    )

    # 6) 의도 플래그
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

    # 7) semantic_query - 제약 표현을 걷어낸 나머지
    semantic = _TEMPORAL_SPAN_RE.sub(" ", working)
    for pattern, _label in SOURCE_TYPE_MARKERS:
        semantic = re.sub(pattern, " ", semantic, flags=re.IGNORECASE)
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
    """규칙 1단계 + (provider가 있으면) LLM 보강 2단계. provider가 None이면 규칙 결과 그대로 -
    평가 하네스가 LLM 없이 기준선을 돌리는 경로.

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
        or constraints.time_range is not None
    ):
        strategies.append(STRATEGY_METADATA_FILTER)
    if constraints.semantic_query:
        strategies.append(STRATEGY_VECTOR)

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
