"""Retrieval Plan 실행기 - `search/constraints.py`가 뽑은 제약 + 전략 이름 목록을 받아 실제
검색(`search/service.py`의 document/lexical/vector 검색, `search/sql.py` 경로의 읽기 전용 SQL,
`search/document.py::read_document`, `search/planner.py`)을 **결정론적으로 조합**하고 하나의
랭킹(`RetrievalResult`)으로 돌려준다 - wiki/search-architecture.md "Retrieval Plan 실행기".

왜 필요한가: 지금까지 "어떤 도구를 어떤 순서로 부를지"는 에이전트(LLM)가 매 턴 암묵적으로
정했다. 그 판단은 (a) 에이전트 turn 없이 검색을 호출해야 하는 소비자 - 골든셋 IR 평가 하네스
(`eval-ir`, wiki/search-evaluation.md (C)) - 에게는 존재하지 않고, (b) 같은 입력에 같은 출력을
보장하지 않아 지표를 비교할 수 없다. 이 모듈은 그 판단을 `QueryConstraints`/`suggest_strategies`
위의 순수 함수 조합으로 꺼낸 것이다: LLM은 (1) 제약 추출 보강(`extract_constraints`)과 (2)
멀티홉 분해(`plan_and_search`)에만 쓰이고, `provider=None`이면 둘 다 건너뛴 채 규칙만으로 돈다.

반환 계약(`RetrievalResult`/`RetrievalStep`)은 `eval-ir` 쪽과 공유하는 약속이다 - 채점은
`document_ids`(hits에서 document_id 첫 등장 순으로 중복 제거)와 `scores`/`predicted_count`만
쓰고, `hits`/`steps`는 사람이 실패 원인을 추적하는 용도다(`evaluation/ir_metrics.score_query`).

전략별 실행 규칙과 병합 결정(anchor 고정, metadata 필터 교집합, RRF)의 이유는 wiki에 표로 적어
두었다. 여기서는 코드가 따르는 순서만 요약한다:

1. 후보 생성: document_lookup(제목/파일명) → lexical(식별자/따옴표 구절) → metadata_filter
   (SQL WHERE, 후보 집합 겸 필터) → vector(semantic_query) → keyword_arm(같은 semantic_query를
   제목/본문에도) → comparison(대상별 제목 조회) → multihop(planner, provider 있을 때만).
   keyword_arm은 vector가 비었을 때의 degrade가 아니라 늘 함께 도는 하이브리드의 반대쪽 팔이다
   (`_Run.keyword_arm`) - 임베딩이 없는 배포에서도 실행기가 "아무것도 못 찾음"이 아니라 키워드
   기준선으로 동작하고, 임베딩이 있어도 어휘 신호가 RRF에 남는다.
2. 병합: 소스별 히트 리스트를 `merge_and_rerank`(RRF)로 합치고, metadata_filter 집합이 있으면
   교집합, document_lookup이 anchor 문서를 특정했으면 그 문서 히트를 최상위로 고정.
3. 후처리: latest(시각 내림차순 + version_family 압축) → whole_document(read_document로 본문
   청크 실음) → exhaustive/count(컷오프 없음, predicted_count).

`agent/tools.py`의 `search` 도구는 이 함수를 얇게 감싼 어댑터다(계층: search/* < agent/*).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Coroutine
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from knowledge_forge.chat_provider import ChatProvider
from knowledge_forge.config import settings
from knowledge_forge.ingest_version import version_sort_key
from knowledge_forge.search.constraints import (
    STRATEGY_COMPARISON,
    STRATEGY_COUNT,
    STRATEGY_DOCUMENT_LOOKUP,
    STRATEGY_EXHAUSTIVE,
    STRATEGY_KEYWORD_ARM,
    STRATEGY_LATEST,
    STRATEGY_LEXICAL,
    STRATEGY_METADATA_FILTER,
    STRATEGY_MULTIHOP,
    STRATEGY_NEGATIVE,
    STRATEGY_VECTOR,
    STRATEGY_WHOLE_DOCUMENT,
    QueryConstraints,
    extract_constraints,
    extract_constraints_rules,
    keyword_terms,
    suggest_strategies,
)
from knowledge_forge.search.document import read_document
from knowledge_forge.search.planner import plan_and_search
from knowledge_forge.search.rerank import DEFAULT_RRF_K, merge_and_rerank
from knowledge_forge.search.service import document_search, lexical_search, vector_search
from knowledge_forge.search.temporal import parse_temporal_expression

if TYPE_CHECKING:
    from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)

# 소스 이름 (RetrievalStep.source / merge_and_rerank 키)
SOURCE_DOCUMENT_SEARCH = "document_search"
SOURCE_LEXICAL_SEARCH = "lexical_search"
# `_Run.keyword_arm`이 semantic_query 통째로 거는 조회의 소스 이름. 같은 검색 함수를
# 쓰지만 질의가 다르므로(전략 쪽은 identifiers/quoted_phrases 단위) 위의 두 소스에
# append하지 않고 별도 투표자로 분리한다 - 한 소스 리스트 안에 서로 다른 질의의 순위를
# 섞으면 RRF의 `1/(k+rank)`가 의미를 잃는다. `_term_fallback`의 `lexical_search#<term>`과
# 같은 관례(`#` 뒤가 "무엇으로 조회했는지").
SOURCE_DOCUMENT_SEARCH_SEMANTIC = f"{SOURCE_DOCUMENT_SEARCH}#semantic"
SOURCE_LEXICAL_SEARCH_SEMANTIC = f"{SOURCE_LEXICAL_SEARCH}#semantic"
SOURCE_VECTOR_SEARCH = "vector_search"
SOURCE_SQL_SEARCH = "sql_search"
SOURCE_PLAN_SEARCH = "plan_search"
SOURCE_READ_DOCUMENT = "read_document"
SOURCE_VERSION_FAMILY = "version_family"
# `_Run.link_cohesion`이 `document_links` 테이블에서 만든 투표자의 이름. 검색이 아니라
# **이미 있는 후보들 사이의 관계**를 읽은 것이라 조회 소스가 아니지만, RRF에 한 표로
# 들어가므로 다른 소스와 같은 이름 공간을 쓴다.
SOURCE_DOCUMENT_LINKS = "document_links"
# `steps`에만 쓰는 이름 - `constraints.py`의 `STRATEGY_*`와 달리 라우팅되는 전략이 아니다.
# 링크 응집도는 후보 생성 단계가 아니라 병합 **뒤**에 얹는 재정렬이라 `suggest_strategies`가
# 고를 대상이 아니고, `strategies_override`로 켜고 끄는 대상도 아니다.
STRATEGY_LINK_COHESION = "link_cohesion"

# --- 임베딩 grain (벡터 arm이 볼 코퍼스) ---------------------------------
#
# `knowledge_object_embeddings`에는 두 grain이 같이 산다(`KF_EMBEDDING_SOURCE`,
# wiki/embeddings.md "청크 기반 vs KnowledgeObject 기반"): 원시 청크 텍스트를 그대로
# 임베딩한 `target_kind='chunk'` 행과, 추출된 KnowledgeObject의 summary/content/topics를
# 임베딩한 `target_kind='knowledge_object'` 행이다. 기본값(None)은 **둘을 한 랭킹에서
# 경쟁**시킨다 - 실사용에서 원하는 동작이고, 지금까지 잰 run이 전부 이 상태다.
#
# 평가에서만 이 축을 고정한다(`eval-ir --grain`). 두 grain이 섞인 채로는 "청크 임베딩이
# 좋아서 오른 건가, KO 임베딩이 좋아서 오른 건가"를 답할 수 없고, 한쪽 백필이 밀려 있으면
# (chunks 2856 / KO 임베딩 936처럼) 그 불균형이 코드 변경의 효과로 잘못 읽힌다.
GRAIN_CHUNK = "chunk"
GRAIN_KNOWLEDGE_OBJECT = "knowledge_object"
# 사람이 치는 표기 → `target_kind` 값. "okf"는 OKF 트리에 파일로 떨어지는 것이 바로
# KnowledgeObject라서 두는 별칭이고(`okf/writer.py::write_objects`), "both"는 "좁히지
# 않음"(= 현행 동작)이다.
GRAIN_ALIASES: dict[str, str | None] = {
    "chunk": GRAIN_CHUNK,
    "chunks": GRAIN_CHUNK,
    "okf": GRAIN_KNOWLEDGE_OBJECT,
    "ko": GRAIN_KNOWLEDGE_OBJECT,
    "knowledge_object": GRAIN_KNOWLEDGE_OBJECT,
    "both": None,
    "all": None,
}


def normalize_grain(value: str | None) -> str | None:
    """사람이 친 grain 표기를 `target_kind`로. 모르는 값은 `ValueError` - 오타를
    조용히 "둘 다"로 흘리면, 좁힌 줄 알고 잰 run이 사실은 안 좁혀진 run이 된다."""
    if value is None:
        return None
    key = value.strip().lower()
    if key not in GRAIN_ALIASES:
        raise ValueError(f"grain은 {sorted(GRAIN_ALIASES)} 중 하나여야 합니다 (받은 값: {value!r})")
    return GRAIN_ALIASES[key]

# exhaustive/negative/count에서 "전부"를 대신하는 상한 - top-k 절단은 없애되 한 질의가 테이블
# 전체를 끌어오지는 못하게(`search/sql.py::DEFAULT_MAX_ROWS`와 같은 200).
EXHAUSTIVE_LIMIT = 200
# metadata_filter 후보 집합의 상한 - 이 집합은 다른 전략 결과의 교집합 필터라 limit보다 훨씬
# 커야 한다(작으면 필터가 아니라 또 하나의 top-k가 된다).
_FILTER_MAX_ROWS = 500
# 소스별 over-fetch 배수 - RRF 병합/필터 교집합/anchor 좁히기 뒤에도 limit개 문서가 남게.
_OVERFETCH = 3
# term 단위 fallback(`_Run._term_fallback`)의 term당 조회 상한. `fetch_limit`을 그대로 쓰지
# 않는다 - exhaustive/negative/count 질의는 `fetch_limit`이 EXHAUSTIVE_LIMIT(=200)이고 최종
# 컷오프도 없어서, term 열 개 × 200이 그대로 답에 실리면 재현율보다 정밀도를 더 깎는다.
_TERM_FETCH_LIMIT = 20
# 한 질의에서 쪼개 쓸 term 개수 상한. term마다 DB 왕복 한 번이라 지연시간과 맞바꾼다.
_MAX_FALLBACK_TERMS = 10
# document_lookup이 후보 하나를 anchor로 확정하는 조건: 1위가 제목 substring 일치(1.0)이고
# 2위와 이만큼 벌어져야 한다. 2위도 1.0이면("AGENTS.md" vs "agents-skills-AGENTS.md") ambiguous.
_ANCHOR_MIN_SCORE = 1.0
_ANCHOR_MIN_GAP = 0.2
# `keyword_arm`이 term 단위 fallback(`_term_fallback`)으로 내려가는 기준 - 질의 문자열 통째로
# 건 두 조회(제목+본문)가 **서로 다른 문서를 이만큼도 못 물어왔으면** 그건 랭킹이 아니라 우연
# 한 건이다. 문서 수로 세는 이유와 값이 2인 이유는 `keyword_arm` 안의 주석에.
_ARM_STRONG_DOCUMENTS = 2
# whole_document에서 anchor가 없을 때 읽는 상위 문서 수, 그리고 한 문서에서 싣는 본문 상한 -
# `agent/tools.py::_READ_DOCUMENT_MAX_CHARS`와 같은 값(그쪽 상수를 import하면 계층이 뒤집힌다).
_WHOLE_DOCUMENT_TOP_N = 2
_WHOLE_DOCUMENT_MAX_CHARS = 50_000
_HIT_SUMMARY_CHARS = 500
# comparison 대상별로 실을 문서 수 - "대상별 최상위 문서"를 하나만 고르면 제목 조회의 tie에서
# 정답이 2위로 밀렸을 때 비교 자체가 무너지므로 약간 여유를 둔다.
_COMPARISON_PER_TARGET = 2
# --- document_links 응집도(`_Run.link_cohesion`) ---------------------------------
# 씨앗으로 삼을 병합 랭킹 상위 문서 수. "관련도 상위 몇 건이 같은 대화에 속하는가"를 묻는
# 것이므로 최종 limit(기본 10)과 같은 자릿수로 둔다 - 더 늘리면 관련도가 낮은 문서의
# 이웃까지 표를 받아 신호가 희석되고, 더 줄이면 대화가 상위 2~3건에 몰린 질의에서만 돈다.
_LINK_SEED_TOP_N = 10
# 따라갈 링크 종류 - **"같은 대화에 속한다"가 참인 것만.** 링크 이름만 보고 고르면 안 된다:
# - `same_channel_sequence`(145행, `context/slack.py`)는 한 **채널 안에서** 인접한 에피소드를
#   잇는다. 채널은 실제 묶음이라 이 신호는 참이다.
# - `thread_sequence`(58행)는 이름과 달리 대화가 아니다. `context/gmail.py`
#   `GmailThreadReconstructor`는 **메일함의 모든 스레드를 도착 시각 순으로 한 줄로** 잇는다
#   (`ordered_threads`의 `previous_uri`) - gmail 문서 하나가 이미 스레드 하나라서, 이 간선이
#   말하는 것은 "이 스레드가 저 스레드 다음에 왔다"뿐이고 내용은 아무 상관이 없다. 실제로
#   이걸 켜고 재면 서로 무관한 "공유 요청" 메일 두 건이 서로를 밀어 올려 정답을 3위로
#   떨어뜨렸다(v0-factoid-001/-ambiguous-001 1.000 → 0.500/0.571,
#   v0-long_range_context-001 1.000 → 0.500). 그래서 뺀다.
# - `previous_version`은 버전 사슬이고, `latest` 후처리(`version_family` 압축)가 이미
#   **반대 방향으로** 다룬다(구버전을 뒤로 내린다) - 여기서 표를 주면 그 압축을 되돌린다.
# - `attachment_of`/`parent_folder`는 오늘 코퍼스에 0행이다.
_LINK_TYPES = ("same_channel_sequence", "thread_parent", "thread_reply")
# 링크 응집도를 **켜는 조건**. `constraints.py`에는 "순서를 묻는 질의"라는 플래그가 없어
# (그 파일은 다른 소유자다) 여기서 직접 본다 - 이 실행기 안에서만 쓰는 판정이라 제약 추출
# 계약을 넓히지 않는다.
#
# 왜 게이트가 필요한가(실측): 게이트 없이 모든 질의에 응집도 표를 주면 채널 클러스터가
# 무관한 질의의 상위를 통째로 가져간다. v0-whole_document_summary-003("AGENTS.md 전체 내용을
# 요약해줘")에서 슬랙 에피소드가 14위/62위에서 4위/9위로 올라와 정답을 밀어냈고(0.631 → 0.000),
# v0-factoid-003에서는 같은 채널 6건이 몰려 올라와 1위 정답이 밀렸다(1.000 → 0.289). 후보가
# 많을수록 나빠지는, `_Run.latest`가 예전에 밟았던 그 붕괴와 같은 종류다.
#
# 왜 어휘 게이트를 써도 되는가: `_is_source_hint_only_filter`가 못 믿는 `source_type` 힌트는
# "정답이 **어디에** 있는지"에 대한 추측이라 틀리면 정답을 잘라낸다. 여기서 보는 것은 "사용자가
# **무엇을** 원하는지"(순서)이고, 오탐이어도 결과는 한 채널 안의 재정렬뿐이라 손실이 갇혀 있다.
_SEQUENCE_INTENT_RE = re.compile(r"순서|순으로|차례|흐름|타임라인|시간순|순차")

# document_types 라벨 중 실제로는 문서 제목이 아니라 소스 종류를 뜻하는 것 - 메일 제목에 "메일"이
# 들어 있을 리 없으니 title ILIKE로 걸면 전부 사라진다.
_DOCUMENT_TYPE_AS_SOURCE: dict[str, str] = {"메일": "gmail"}


@dataclass
class RetrievalStep:
    """실행기가 실제로 부른 검색 하나의 기록. `detail`은 직렬화 가능한 값만(SQL 문자열, 필터 값,
    fallback 여부 등) - 평가 리포트/도구 결과에 그대로 실린다."""

    strategy: str
    source: str
    n_hits: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    query: str
    constraints: dict[str, Any]
    strategies: list[str]
    steps: list[RetrievalStep]
    hits: list[dict[str, Any]]
    document_ids: list[str]
    scores: list[float]
    predicted_count: int | None
    documents_read: list[str]
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- SQL 리터럴 헬퍼 ------------------------------------------------------------
# `PipelineStore.execute_readonly_query`는 파라미터 바인딩 없이 SQL 문자열만 받는다(모델이 쓴
# SQL을 그대로 돌리는 `sql_search`용이라 `%` 플레이스홀더 스캔 자체를 피한다). 그래서 값은 여기서
# 표준 SQL 이스케이프(`'` → `''`)로 인라인한다 - 실행 자체는 READ ONLY 트랜잭션 + 강제 롤백
# (`postgres_store.py`)이라 최악의 경우에도 쓰기는 불가능하다.


def _sql_literal(value: str) -> str:
    return "'" + value.replace("\x00", "").replace("'", "''") + "'"


def _like_literal(value: str) -> str:
    """`'%value%'` - LIKE 메타문자(`\\`, `%`, `_`)를 이스케이프해서. 파일명은 `_`투성이라 이걸
    빼면 `_`가 한 글자 와일드카드로 풀린다(`postgres_store.py::_like_pattern`과 같은 이유)."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return _sql_literal(f"%{escaped}%")


def _suffix_like_literal(suffix: str) -> str:
    r"""`'%<suffix>'` - 접미사(파일 확장자) 전용. `_like_literal`을 쓰면 안 된다 - 그쪽은
    값 전체를 이스케이프한 뒤 양옆에 `%`를 붙이므로, 앞에 `%`를 직접 붙여 넘기면 그 `%`까지
    literal로 이스케이프돼 `'%\%.xlsx%'`가 된다(실측: 필터가 0행). 여기서는 값만
    이스케이프하고 와일드카드는 **우리가** 붙인다."""
    escaped = suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return _sql_literal(f"%{escaped}")


def _timestamp_literal(value: datetime) -> str:
    return f"{_sql_literal(value.isoformat())}::timestamptz"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _unique(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


@dataclass
class _KeywordArmHits:
    """`_Run._keyword_arm_fetch`가 미리 던져 둔 두 조회의 원본 결과 - `_keyword_arm`이
    받아서 anchor 판정/좁히기를 한다. 튜플 대신 이름을 붙인 이유는 둘 다 같은 모양의
    히트 리스트라 순서를 뒤집어도 타입 검사에 안 걸리기 때문이다(제목/본문이 바뀌면
    anchor를 청크 본문 일치에서 잡게 되는 조용한 회귀)."""

    title: list[dict[str, Any]]
    body: list[dict[str, Any]]


async def _capture_exception(
    coro: Coroutine[Any, Any, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], BaseException | None]:
    """코루틴을 돌리고 (결과, 예외) 쌍으로 돌려준다 - `asyncio.gather`에 실패해도 되는
    코루틴을 넣을 때 쓴다.

    `gather(..., return_exceptions=True)`를 쓰지 않는 이유: 그건 **모든** 인자에 적용돼서
    같이 묶인 코루틴(어휘 arm)의 실제 버그까지 조용히 값으로 바꿔 버린다. 실패를 허용하는
    쪽은 벡터 조회 하나뿐이고(임베더 미구성/원격 장애), 나머지는 예전처럼 그대로 터져야
    한다."""
    try:
        return await coro, None
    except Exception as exc:  # noqa: BLE001 - 호출자가 로깅/기록을 결정한다
        return [], exc


def _hit_score(hit: dict[str, Any]) -> float:
    score = hit.get("score")
    return float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0


def _hit_time(hit: dict[str, Any]) -> str:
    """정렬용 시각 문자열(ISO, 비어 있으면 가장 오래된 것으로) - 모든 소스가 `content_at`
    (COALESCE(modified_at, collected_at))을 싣지만 planner 경유 file_search 히트는 없다."""
    value = hit.get("content_at") or hit.get("modified_at")
    return str(value) if value else ""


def _dedupe_documents(hits: list[dict[str, Any]]) -> tuple[list[str], list[float]]:
    ids: list[str] = []
    best: dict[str, float] = {}
    for hit in hits:
        document_id = hit.get("document_id")
        if not document_id:
            continue
        score = _hit_score(hit)
        if document_id not in best:
            ids.append(document_id)
            best[document_id] = score
        elif score > best[document_id]:
            best[document_id] = score
    return ids, [best[i] for i in ids]


def _with_document_id(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """document_id가 없는 히트(planner 경유 file_search의 OKF 파일 히트 등)는 채점 단위가 없어
    버린다 - 계약상 모든 hit에 document_id가 있어야 한다."""
    return [hit for hit in hits if hit.get("document_id")]


def _narrow_to_anchor(hits: list[dict[str, Any]], anchor: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """anchor 문서가 있으면 그 문서의 히트만 남긴다. 하나도 없으면(그 문서 본문에 식별자가 없거나
    임베딩이 없음) 좁히지 않고 그대로 둔다 - 빈 결과보다 넓은 결과가 낫고, 사유는 detail에 남긴다."""
    if anchor is None:
        return hits, None
    narrowed = [hit for hit in hits if hit.get("document_id") == anchor]
    if narrowed:
        return narrowed, "narrowed"
    return hits, "no_hits_in_anchor"


# --- metadata_filter SQL ----------------------------------------------------------


def _metadata_filter_where(constraints: QueryConstraints, project_id: str | None) -> list[str]:
    """`sources`/`documents`에 거는 WHERE 조건 목록(AND). 어느 키를 보는지는 개발 DB의 실제
    `sources.metadata` 모양에서 정했다: gmail은 `participants`(["이름 <주소>", ...])/`subject`,
    slack은 `participants`(user id 배열)/`channel`, google_drive는 `name`뿐이다. 작성자/참여자는
    둘 다 `participants` 배열의 JSON 텍스트에 ILIKE - `->>`가 돌려주는 배열 텍스트에 이름이 그대로
    들어 있고, `jsonb_array_elements_text`는 배열이 아닌 값에서 에러가 나 방어 코드가 더 든다.
    `documents.metadata->>'author'`(파서가 뽑은 작성자)도 같이 본다. Slack 참여자는 user id라
    이름 필터가 닿지 않는다(알려진 한계)."""
    where: list[str] = []
    if project_id:
        where.append(f"d.project_id = {_sql_literal(project_id)}")

    source_types = list(constraints.source_types)
    for label in constraints.document_types:
        mapped = _DOCUMENT_TYPE_AS_SOURCE.get(label)
        if mapped and mapped not in source_types:
            source_types.append(mapped)
    if source_types:
        where.append("s.source_type IN (" + ", ".join(_sql_literal(t) for t in source_types) + ")")

    # 파일 종류. Drive 소스만 `name`을 들고 있으므로 다른 소스 타입은 이 술어에 절대 안 걸린다 -
    # 그건 의도다("xlsx 파일"을 물으면 슬랙 스레드는 답이 아니다).
    if constraints.file_extensions:
        where.append(
            "("
            + " OR ".join(
                f"s.metadata->>'name' ILIKE {_suffix_like_literal(ext)}" for ext in constraints.file_extensions
            )
            + ")"
        )
    if constraints.mime_types:
        where.append(
            "s.metadata->>'mimeType' IN (" + ", ".join(_sql_literal(m) for m in constraints.mime_types) + ")"
        )

    if constraints.time_range is not None:
        if constraints.time_range.start is not None:
            where.append(f"COALESCE(s.modified_at, s.collected_at) >= {_timestamp_literal(constraints.time_range.start)}")
        if constraints.time_range.end is not None:
            where.append(f"COALESCE(s.modified_at, s.collected_at) < {_timestamp_literal(constraints.time_range.end)}")

    def _person(name: str) -> str:
        pattern = _like_literal(name)
        # `participants`는 소스마다 모양이 다르다: gmail은 "이름 <주소>" 문자열이라 이름이 그대로
        # 들어 있지만 slack은 유저 ID 배열(["U0…"])이라 사람 이름이 절대 닿지 않는다.
        # `participant_names`는 SlackDirectory가 그 ID를 사람이 읽는 이름으로 푼 배열이다
        # (collectors/saas/slack.py, wiki/saas-connectors.md). 원시 `participants`는 조인 키로
        # 그대로 남겨 두므로 여기서는 **둘 다** 본다 - 어느 한쪽만 보면 소스 하나가 통째로 빠진다.
        return (
            f"(s.metadata->>'participants' ILIKE {pattern}"
            f" OR s.metadata->>'participant_names' ILIKE {pattern}"
            f" OR d.metadata->>'author' ILIKE {pattern})"
        )

    # 작성자는 "그중 하나"(OR), 참여자는 "전부 참석"(AND) - "A와 B의 회의"는 둘 다 있어야 한다.
    if constraints.authors:
        where.append("(" + " OR ".join(_person(name) for name in constraints.authors) + ")")
    for name in constraints.participants:
        where.append(_person(name))

    # `document_types` 중 소스로 매핑되지 않는 라벨("회의록"/"매뉴얼"/"보고서"…)에는 **술어를
    # 걸지 않는다.** 예전에는 `d.title ILIKE '%매뉴얼%'`로 걸었는데, 제목은 사람이 읽는 문자열이지
    # 타입 라벨이 아니다 - 제목에 본문이 조금이라도 섞이면 곧바로 오탐이고, 이 필터는
    # `_is_source_hint_only_filter`가 "믿을 만한 구조적 술어"로 분류해 **교집합**을 잡으므로
    # 오탐 1건이 결과 전체를 대체한다. 근거와 기각한 대안은 그 함수 docstring에.
    return where


def _is_source_hint_only_filter(constraints: QueryConstraints) -> bool:
    """이 필터가 "어디서(source_type)"만 담고 "누가/언제/무엇"은 하나도 담지 않았는가.

    `merge()`가 **필터를 믿을지 관련도 랭킹을 믿을지** 가르는 신뢰도 판정이다.
    `_metadata_filter_where`가 만드는 WHERE 절과 1:1로 대응하므로 **둘은 같이 고쳐야 한다.**

    가르는 기준이 "규칙 vs LLM"이 아닌 이유: `source_types`는 애초에 LLM 보강 대상이 아니다
    (`constraints.LLM_ENRICHABLE_FIELDS`에 없다). 그래서 provenance로는 갈리지 않고, 대신
    **술어의 종류**로 가른다 -
    - 작성자/참여자(`participants ILIKE '%nonghyup.com%'`), 시간 범위, 제목 종류는 질의가 명시한
      구조적 술어다. 사용자가 "농협 도메인에서 온 메일"이라고 했으면 그 도메인 조건은 틀릴 수
      없다. 이런 질의는 semantic_query가 "보낸" 한 단어로 쪼그라들어 관련도 arm이 오히려 쓰레기다.
    - 반면 `source_type` 하나뿐인 필터는 `SOURCE_TYPE_MARKERS` 정규식이 질의 표면 어휘만 보고
      찍은 추측이고, 그 어휘→source_type 매핑은 방향이 자주 반대다(merge() 주석의 코퍼스 실측).

    **"제목 종류"가 이 목록에서 빠진 경위**(2026-09-05). 예전에는 `document_types` 중 소스로
    매핑되지 않는 라벨을 `d.title ILIKE '%매뉴얼%'`로 걸고 그걸 구조적 술어로 쳤다. 그 술어는
    **제목을 문서 종류 라벨로 쓴다**는 잘못된 전제 위에 있었고, 슬랙 제목에 첫 메시지 발췌를
    붙이자마자 실측으로 무너졌다(`fix/slack-channel-names`의 `_SLACK_EXCERPT_CHARS`):
    `v0-whole_document_summary-003` ndcg 0.631→0.000("[done] 운영 매뉴얼 초안" 스레드가
    `%매뉴얼%`에 걸림), `v0-negative-001`("Kubernetes 클러스터 구성 가이드") none_correct
    1.0→0.0. **negative 질의까지 깨진다**는 게 핵심이다 - 오탐 1건이 교집합을 잡아 결과
    전체를 대체하기 때문이다.

    "신뢰할 수 있는 출처로 옮긴다"를 먼저 시도했고 **기각했다.** 개발 DB 실측:
    - `documents.metadata`에 `document_type` 키는 **없다**(358행 전부. 있는 키는 created_at/
      page_count/author/language/title/version_*).
    - `knowledge_objects.document_type`은 있지만 **LLM이 매번 자유 문자열로 쓴 값**이라
      5107행에 서로 다른 값이 **571가지**다("Slack 메시지"/"Slack 대화"/"슬랙 메시지"/
      "Slack message"/"guide"/"guide or manual"…). `DOCUMENT_TYPE_MARKERS`의 8개 라벨로
      정규화할 방법이 없고, 정규화한들 KO가 붙은 문서가 **263/358**뿐이라 나머지 95건이
      "타입 불명"이 아니라 "타입 불일치"로 **하드 필터에서 통째로 사라진다.** 하드 필터의
      입력으로 쓰기에는 정밀도도 커버리지도 모자란다.
    그래서 술어 자체를 없앴다. 타입 라벨은 그동안에도 `semantic_query`를 통해 제목/본문/벡터
    arm에 이미 들어가고 있다("회의록 요약해줘"의 "회의록"은 `document_search`가 본다) - 잃은
    것은 **하드 필터로서의** 타입 조건뿐이다.
    """
    if not constraints.source_types and not any(t in _DOCUMENT_TYPE_AS_SOURCE for t in constraints.document_types):
        return False
    # 확장자/MIME 타입은 `source_type` 추측과 달리 **질의가 명시한 구조적 술어**다
    # ("xlsx 파일 전부"에서 .xlsx는 틀릴 수 없다). 그래서 이게 있으면 "출처 힌트뿐"이 아니다.
    return not (
        constraints.authors
        or constraints.participants
        or constraints.time_range
        or constraints.file_extensions
        or constraints.mime_types
    )


def _has_metadata_predicate(constraints: QueryConstraints) -> bool:
    """`_metadata_filter_where`가 `project_id` 말고 **실제 술어를 하나라도** 만드는가.

    `suggest_strategies`는 `document_types`가 있으면 metadata_filter를 고르는데, 소스로
    매핑되지 않는 타입 라벨은 이제 술어를 만들지 않는다(위 함수 참고). 그대로 두면 WHERE가
    `project_id` 하나뿐인 질의가 코퍼스 전체(`_FILTER_MAX_ROWS`)를 긁어와 아무것도 안 거르는
    교집합을 잡는다 - 왕복만 낭비하고 `filter_only`/`no_overlap` 분기의 의미도 흐려진다."""
    return bool(
        constraints.source_types
        or any(t in _DOCUMENT_TYPE_AS_SOURCE for t in constraints.document_types)
        or constraints.authors
        or constraints.participants
        or constraints.time_range is not None
        or constraints.file_extensions
        or constraints.mime_types
    )


_FILTER_FROM = "FROM documents d JOIN sources s ON s.source_id = d.source_id"


def _metadata_filter_sql(where: list[str]) -> str:
    clause = " AND ".join(where) if where else "TRUE"
    return (
        "SELECT d.document_id, d.title, s.source_type, s.uri, s.modified_at, "
        "COALESCE(s.modified_at, s.collected_at) AS content_at, d.project_id\n"
        f"{_FILTER_FROM}\nWHERE {clause}\n"
        "ORDER BY content_at DESC NULLS LAST, d.document_id"
    )


def _metadata_count_sql(where: list[str]) -> str:
    clause = " AND ".join(where) if where else "TRUE"
    return f"SELECT COUNT(*) AS n\n{_FILTER_FROM}\nWHERE {clause}"


# --- document_links SQL -----------------------------------------------------------


def _document_links_sql(document_ids: list[str]) -> str:
    """씨앗 문서들에 걸린 `_LINK_TYPES` 링크를 양방향으로 한 번에 읽는다.

    방향을 무시하는 이유: `same_channel_sequence`/`thread_sequence`의 방향은 "누가 먼저였나"
    (시간 순서)이지 "누가 누구에 속하나"가 아니다. 여기서 쓰는 신호는 **같은 대화에 속한다**는
    무방향 사실 하나뿐이고, 시간 순서는 이미 모든 히트가 싣고 있는 `content_at`이 더 정확하게
    준다(링크는 인접한 두 건만 잇지만 `content_at`은 전순서를 준다). 그래서 링크에서 정렬을
    다시 꺼내려 하지 않는다 - 링크가 timestamp보다 더 아는 것은 오직 "묶음"이다."""
    values = ", ".join(_sql_literal(i) for i in document_ids)
    types = ", ".join(_sql_literal(t) for t in _LINK_TYPES)
    return (
        "SELECT from_document_id, to_document_id, link_type\n"
        "FROM document_links\n"
        f"WHERE link_type IN ({types})\n"
        f"  AND (from_document_id IN ({values}) OR to_document_id IN ({values}))"
    )


def _filter_row_to_hit(row: tuple[Any, ...]) -> dict[str, Any]:
    document_id, title, source_type, uri, modified_at, content_at, project_id = row
    return {
        "target_kind": "document",
        "document_id": document_id,
        "title": title,
        "source_type": source_type,
        "uri": uri,
        "modified_at": _iso(modified_at) if isinstance(modified_at, datetime) else modified_at,
        "content_at": _iso(content_at) if isinstance(content_at, datetime) else content_at,
        "project_id": project_id,
        "score": 1.0,
    }


def _coverage_threshold(n_terms: int) -> int:
    """term 단위 fallback에서 후보로 남기려면 질의의 term을 몇 개나 담아야 하는가.

    필터가 없으면 term 하나만 스친 문서까지 전부 후보가 된다 - 재현율은 오르지만 "관련 문서
    없음"이 정답인 질의(null/negative)가 전부 무언가를 돌려주고, 개수(count) 질의가 부풀고,
    navigational 질의의 정답이 노이즈에 밀린다(첫 term-fallback 실측에서 none_correct
    1.000 → 0.000, count_match 0.500 → 0.000). 비율 하나로는 term이 두세 개뿐인 짧은 질의에서
    1개만 겹쳐도 통과하므로 `term_coverage_min`을 하한으로 둔다. 단, term 수 자체를 넘을 수는
    없다 - 그러면 아무것도 통과하지 못한다."""
    ratio = max(0.0, settings.term_coverage_ratio)
    floor = max(1, settings.term_coverage_min)
    return min(n_terms, max(floor, math.ceil(n_terms * ratio)))


def _term_document_frequency_sql(
    terms: list[str], *, project_id: str | None, since: datetime | None, until: datetime | None
) -> str:
    """term별 문서 빈도(df)를 **한 번의 읽기 전용 질의**로 세는 SQL - `_required_terms`의 입력.

    term당 `lexical_search`를 이미 부르고 있으니 그 히트에서 df를 세면 추가 질의가 0회지만,
    그 값은 못 쓴다: `search_chunks_lexical`은 `LIMIT`(여기서는 `_TERM_FETCH_LIMIT`=20)을
    `is_substring DESC, sim DESC, document_id` 순으로 자르기 때문에, 흔한 term일수록 상위 20
    청크가 **같은 문서 몇 개에 몰려** df가 극단적으로 과소평가된다(실측: "인터뷰" 절단 df=3 /
    실제 37, "OCR" 1 / 34, "risk-agent" 4 / 51). v0 골든셋의 per_term 질의 26개 중 7개에서
    "가장 희귀한 term"의 신원이 뒤집혔다 - 판정 자체가 바뀌므로 별도 count가 필요하다.

    substring(ILIKE) 티어만 센다. `search_chunks_lexical`의 3티어(substring/전 토큰/
    word_similarity)를 여기서 복제하면 `storage/`의 매칭 규칙을 두 곳에 두게 되고, df는
    "이 term이 코퍼스에서 얼마나 흔한가"의 척도라 가장 정확하고 인덱스로 답할 수 있는 티어면
    충분하다. fuzzy 티어로만 걸린 문서는 호출부에서 실측 df(그 term의 히트에 실제로 등장한
    문서 수)와 `max`를 취해 메운다 - 그래야 "히트는 있는데 df=0" 같은 모순이 생기지 않는다."""
    where: list[str] = []
    joins = ""
    if project_id:
        joins += "\n  JOIN documents d ON d.document_id = c.document_id"
        where.append(f"d.project_id = {_sql_literal(project_id)}")
    if since is not None or until is not None:
        if not project_id:
            joins += "\n  JOIN documents d ON d.document_id = c.document_id"
        joins += "\n  JOIN sources s ON s.source_id = d.source_id"
        if since is not None:
            where.append(f"COALESCE(s.modified_at, s.collected_at) >= {_timestamp_literal(since)}")
        if until is not None:
            where.append(f"COALESCE(s.modified_at, s.collected_at) < {_timestamp_literal(until)}")
    scope = ("\n  AND " + "\n  AND ".join(where)) if where else ""
    branches = [
        f"SELECT {index} AS term_index, COUNT(DISTINCT c.document_id) AS df\n"
        f"  FROM chunks c{joins}\n"
        f"  WHERE c.text ILIKE {_like_literal(term)}{scope}"
        for index, term in enumerate(terms)
    ]
    return "\nUNION ALL\n".join(branches)


def _required_terms(terms: list[str], df: dict[str, int]) -> set[str]:
    """coverage 판정에서 **반드시** 담고 있어야 하는 term들 - 질의에서 가장 변별력 있는 것들.

    왜 개수만으로는 안 되나: `_coverage_threshold`는 매칭된 term의 **개수**만 센다. 그래서
    "Kubernetes 클러스터 구성 가이드"(negative)에서 흔한 term 세 개("클러스터/구성/가이드")가
    우연히 한 문서에 겹치면 임계값을 넘어 버린다. 개수 기준은 코퍼스가 커질수록 필연적으로
    나빠진다 - 흔한 term끼리 우연히 겹치는 문서는 코퍼스에 비례해 늘어나기 때문이다(실측:
    코드 변경 없이 263 → 326 문서에서 none_correct 1.000 → 0.667).

    그래서 "질의에서 가장 희귀한 term(들)을 그 문서가 담고 있어야 한다"를 AND 조건으로 건다.
    df가 최소인 term이 여럿이면(동점) **전부** 요구한다 - negative-001이 정확히 그 경우다
    (Kubernetes df=1, 클러스터 df=1이 서로 **다른** 문서에 있어, 하나만 요구하면 노이즈
    문서가 살아남는다). `term_rare_df_max`는 최소 df 위에 얹는 절대 하한이다(0이면 순수하게
    "가장 희귀한 term"만, 값을 올리면 df가 그 이하인 term을 전부 요구해 더 조인다).

    df=0인 term(코퍼스에 아예 없는 개념: "연말정산", "블록체인")은 어떤 문서도 담을 수 없으므로
    이 규칙에서는 **전부 기각**이 된다 - 의도한 동작이다. 코퍼스에 없는 것을 물었으면 "없다"가
    정답이다. 재현율 쪽 위험(오타/동의어로 df=0이 되는 질의)은 호출부에서 실측 df와 `max`를
    취해 완화한다: fuzzy 티어로라도 걸린 문서가 있으면 df ≥ 1이 되어 veto가 아니라 "그 문서를
    요구"로 바뀐다."""
    cap = settings.term_rare_df_max
    if cap < 0:
        return set()
    floor = min(df.get(term, 0) for term in terms)
    limit = max(floor, cap)
    return {term for term in terms if df.get(term, 0) <= limit}


# --- 실행기 ----------------------------------------------------------------------


class _Run:
    """한 번의 `retrieve()` 호출 상태 - 전략 함수들이 공유하는 store/제약/한도와, 소스별 히트
    리스트(`results_by_source`, RRF 입력)·steps·anchor·필터 집합을 모은다. 함수 인자로 열 개씩
    돌리는 대신 객체 하나에 담았을 뿐, 호출 밖으로 살아남지 않는다."""

    def __init__(
        self,
        *,
        store: PipelineStore,
        constraints: QueryConstraints,
        strategies: list[str],
        project_id: str | None,
        now: datetime,
        limit: int,
        grain: str | None = None,
    ) -> None:
        self.store = store
        self.c = constraints
        self.strategies = strategies
        self.project_id = project_id
        self.now = now
        self.limit = limit
        # None = 두 grain 경쟁(현행). 값이 있으면 벡터 조회 전부(이 실행기의 `vector()`와
        # planner 경유 멀티홉)가 그 `target_kind`만 본다 - 한 군데만 좁히면 "순수 grain"이
        # 아니라 "일부만 좁힌" run이 되고, 그 run으로 잰 숫자는 비교 대상이 없다.
        self.grain = grain
        self.wide = any(s in strategies for s in (STRATEGY_EXHAUSTIVE, STRATEGY_NEGATIVE, STRATEGY_COUNT))
        self.fetch_limit = EXHAUSTIVE_LIMIT if self.wide else limit * _OVERFETCH
        self.since = constraints.time_range.start if constraints.time_range else None
        self.until = constraints.time_range.end if constraints.time_range else None
        self.results_by_source: dict[str, list[dict[str, Any]]] = {}
        self.steps: list[RetrievalStep] = []
        self.anchor: str | None = None
        self.pinned: list[str] = []
        self.filter_ids: set[str] | None = None
        self.filter_hits: list[dict[str, Any]] = []
        self.filter_where: list[str] = []
        self.filter_step: RetrievalStep | None = None
        self.documents_read: list[str] = []
        self.predicted_count: int | None = None
        # `vector()`가 어휘 arm 몫으로 미리 던져 둔 조회 결과(`keyword_arm()`이 소비).
        # None이면 arm이 자기 조회를 직접 던진다 - `vector` 없이 arm만 도는 실행일 때다.
        self.arm_fetched: _KeywordArmHits | None = None

    def add_hits(self, source: str, hits: list[dict[str, Any]]) -> None:
        self.results_by_source.setdefault(source, []).extend(hits)

    def step(self, strategy: str, source: str, n_hits: int, **detail: Any) -> RetrievalStep:
        step = RetrievalStep(strategy=strategy, source=source, n_hits=n_hits, detail=detail)
        self.steps.append(step)
        return step

    # -- 후보 생성 ------------------------------------------------------------

    def _claim_anchor(self, title_hits: list[dict[str, Any]]) -> tuple[str, list[tuple[str, float]]]:
        """`document_search` 결과에서 anchor를 판정해 `self.anchor`에 싣고
        (판정 사유, 문서별 최고 점수 랭킹)을 돌려준다.

        anchor는 "제목이 이 질의와 사실상 같은 문서 하나"라는 뜻이라, `merge()`가 그 문서를
        최상위로 고정하고 다른 전략들도 그 안으로 좁힌다(`_narrow_to_anchor`). 판정 기준이
        `>= 1.0`(제목 substring 완전 일치) + 2위와 `0.2` 이상 격차로 빡빡한 이유가 그것이다 -
        "AGENTS.md" vs "agents-skills-AGENTS.md"처럼 둘 다 1.0이면 ambiguous로 두고 포기한다.

        `document_lookup`과 `keyword_arm`이 공유한다. 원래는 `document_lookup`에만 있어서
        **fallback(지금의 arm)이 물어온 제목 1.0 일치는 anchor가 되지 못하고 RRF 한 표로
        전락했다.** arm을 상시화하자 그 구멍이 v0에서 그대로 드러났다 -
        `v0-navigational-002`("codex README")/`-004`/`v0-version-001`이 baseline에서 1위였다가
        2~3위로 밀렸다(ndcg 1.000 → 0.631/0.500). 제목이 정확히 맞은 문서는 벡터 유사도가
        무엇을 말하든 그 질의의 답이다.

        점수는 **문서 단위 최고값**으로 접는다 - 같은 문서가 여러 질의에서 나와도 한 후보다."""
        best: dict[str, float] = {}
        for hit in title_hits:
            document_id, score = hit["document_id"], _hit_score(hit)
            if document_id not in best or score > best[document_id]:
                best[document_id] = score
        ranked = sorted(best.items(), key=lambda item: -item[1])
        if not ranked:
            return "no_candidates", ranked
        top_id, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else None
        if top_score >= _ANCHOR_MIN_SCORE and (second is None or top_score - second >= _ANCHOR_MIN_GAP):
            self.anchor = top_id
            return "single_substring_match", ranked
        return ("ambiguous" if top_score >= _ANCHOR_MIN_SCORE else "weak_match"), ranked

    async def document_lookup(self) -> None:
        queries = _unique([self.c.document_reference, *self.c.identifiers, *self.c.quoted_phrases])
        # 질의끼리는 서로의 결과에 의존하지 않으므로 DB 왕복을 겹친다. `gather`는 입력
        # 순서대로 결과를 돌려주므로(`as_completed`가 아니다) `collected`에 쌓이는 순서는
        # 순차 루프와 **정확히 같다** - 아래 `_claim_anchor`의 tie 처리와 RRF의 순위가 둘 다
        # 리스트 위치에 걸려 있어서, 이 성질이 없으면 지표가 흔들린다.
        per_query = await asyncio.gather(
            *(
                document_search(
                    q, self.store, project_id=self.project_id, limit=self.fetch_limit, since=self.since, until=self.until
                )
                for q in queries
            )
        )
        collected: list[dict[str, Any]] = [hit for hits in per_query for hit in hits]
        anchor_reason, ranked = self._claim_anchor(collected)
        self.add_hits(SOURCE_DOCUMENT_SEARCH, collected)
        self.step(
            STRATEGY_DOCUMENT_LOOKUP,
            SOURCE_DOCUMENT_SEARCH,
            len(collected),
            queries=queries,
            anchor=self.anchor,
            anchor_reason=anchor_reason,
            candidates=[{"document_id": d, "score": s} for d, s in ranked[:5]],
        )

    async def lexical(self) -> None:
        terms = _unique([*self.c.identifiers, *self.c.quoted_phrases])
        # `document_lookup`과 같은 이유로 term별 왕복을 겹친다(gather는 입력 순서 보존).
        # anchor 좁히기는 전부 모은 뒤에 하므로 결과가 달라지지 않는다.
        per_term = await asyncio.gather(
            *(
                lexical_search(
                    term, self.store, project_id=self.project_id, limit=self.fetch_limit, since=self.since, until=self.until
                )
                for term in terms
            )
        )
        collected: list[dict[str, Any]] = [hit for hits in per_term for hit in hits]
        collected, narrowing = _narrow_to_anchor(collected, self.anchor)
        self.add_hits(SOURCE_LEXICAL_SEARCH, collected)
        self.step(STRATEGY_LEXICAL, SOURCE_LEXICAL_SEARCH, len(collected), terms=terms, anchor_narrowing=narrowing)

    async def metadata_filter(self) -> None:
        if not _has_metadata_predicate(self.c):
            self.step(
                STRATEGY_METADATA_FILTER,
                SOURCE_SQL_SEARCH,
                0,
                skipped="no_predicate",
                filters={"document_types": self.c.document_types},
            )
            return
        where = _metadata_filter_where(self.c, self.project_id)
        sql = _metadata_filter_sql(where)
        rows, _columns = await self.store.execute_readonly_query(sql, _FILTER_MAX_ROWS)
        self.filter_where = where
        self.filter_hits = [_filter_row_to_hit(row) for row in rows]
        self.filter_ids = {hit["document_id"] for hit in self.filter_hits}
        self.filter_step = self.step(
            STRATEGY_METADATA_FILTER,
            SOURCE_SQL_SEARCH,
            len(self.filter_hits),
            sql=sql,
            filters={
                "authors": self.c.authors,
                "participants": self.c.participants,
                "source_types": self.c.source_types,
                "document_types": self.c.document_types,
                "since": _iso(self.since),
                "until": _iso(self.until),
            },
            truncated=len(rows) >= _FILTER_MAX_ROWS,
        )

    async def vector(self) -> None:
        query = self.c.semantic_query
        hits: list[dict[str, Any]] = []
        error: str | None = None
        if query:
            vector_call = vector_search(
                query,
                self.store,
                project_id=self.project_id,
                limit=self.fetch_limit,
                since=self.since,
                until=self.until,
                target_kind=self.grain,
            )
            # 벡터 조회와 어휘 arm(`keyword_arm`)의 DB 왕복 두 개는 서로의 결과를 안 본다 -
            # 셋을 겹쳐서 던진다. 이게 이 파일에서 가장 큰 지연시간 항목이다: 벡터 조회에는
            # 원격 임베딩 왕복(실측 ~300ms, `embedding/cache.py` 참고)이 들어 있고, 지금까지는
            # 그 300ms 동안 나머지 두 조회가 그냥 기다렸다.
            #
            # **arm이 이번 실행의 전략 목록에 있을 때만** 미리 던진다. arm은 `retrieve()`가
            # 전략 이름으로 부르는 독립 전략이고(그래서 `strategies_override=["vector"]`가
            # 진짜 순수 벡터다), 여기서 무조건 던지면 그 순수성이 다시 깨진다 - 결과를 안
            # 쓰더라도 제목/본문 조회가 실제로 나가기 때문이다.
            #
            # **결과 처리 순서는 바꾸지 않는다.** anchor는 이 실행기의 결정론이 걸린 상태라
            # (`keyword_arm`이 anchor를 claim할 수 있고, 벡터 히트는 anchor로 좁혀진다)
            # 해석은 `retrieve()`가 vector → arm 순으로, 순차 실행이던 때와 똑같은 순서로
            # 한다. 겹치는 것은 "질의를 언제 던지느냐"뿐이고 "결과를 언제 읽느냐"가 아니다.
            if STRATEGY_KEYWORD_ARM in self.strategies:
                (vector_hits, vector_error), self.arm_fetched = await asyncio.gather(
                    _capture_exception(vector_call), self._keyword_arm_fetch(query)
                )
            else:
                vector_hits, vector_error = await _capture_exception(vector_call)
            # 임베더 미구성/원격 장애가 실행기 전체를 막으면 안 된다 - 예전의 try/except와
            # 같은 의미이고, 잡는 위치만 `_capture_exception`으로 옮겼다(그 docstring 참고).
            if vector_error is not None:
                logger.warning("vector_search failed, continuing without it: %s", vector_error)
                error = f"{type(vector_error).__name__}: {vector_error}"
            else:
                hits = vector_hits
        hits, narrowing = _narrow_to_anchor(hits, self.anchor)
        # 관련도 하한 - config.py의 vector_min_score 주석 참고. anchor 좁히기 뒤에 거는 이유는
        # anchor가 이미 "제목이 정확히 맞은 문서"라는 더 강한 신호라, 그 안에서 유사도가 낮다고
        # 버리면 정답을 잃기 때문이다.
        floor = settings.vector_min_score
        dropped = 0
        if floor > 0.0 and hits:
            kept = [hit for hit in hits if _hit_score(hit) >= floor]
            dropped = len(hits) - len(kept)
            hits = kept
        self.add_hits(SOURCE_VECTOR_SEARCH, hits)
        self.step(
            STRATEGY_VECTOR,
            SOURCE_VECTOR_SEARCH,
            len(hits),
            query=query,
            anchor_narrowing=narrowing,
            error=error,
            min_score=floor,
            dropped_below_min_score=dropped,
            grain=self.grain,
        )

    async def _keyword_arm_fetch(self, query: str) -> _KeywordArmHits:
        """`_keyword_arm`이 쓸 두 조회를 **던지기만** 한다 - 해석은 `_keyword_arm`이 한다.

        둘로 나눈 이유는 오직 하나, `vector()`가 이 두 왕복을 벡터 조회(임베딩 왕복 포함)와
        겹칠 수 있게 하려고다. 제목/본문 조회는 서로도, 벡터 조회와도 입력이 독립이다 -
        anchor를 보는 것은 반환된 히트를 좁힐 때뿐이고, 그건 전부 `_keyword_arm` 안에서
        예전과 같은 순서로 일어난다."""
        title_hits, body_hits = await asyncio.gather(
            document_search(
                query, self.store, project_id=self.project_id, limit=self.fetch_limit, since=self.since, until=self.until
            ),
            lexical_search(
                query, self.store, project_id=self.project_id, limit=self.fetch_limit, since=self.since, until=self.until
            ),
        )
        return _KeywordArmHits(title=title_hits, body=body_hits)

    async def keyword_arm(self) -> None:
        """semantic_query를 제목(`document_search`)과 청크 원문(`lexical_search`)에도 걸어
        RRF에 **벡터와 나란히** 넣는 어휘 arm. 이름 그대로 하이브리드 검색의 한쪽 팔이지,
        벡터가 실패했을 때의 degrade가 아니다.

        호출은 `retrieve()`가 전략 목록(`STRATEGY_KEYWORD_ARM`)을 보고 한다. 예전에는
        `vector()` 끝에서 무조건 이어 부르는 **부작용**이었고, 그래서 전략 목록에 이름이
        없었다 - `strategies_override=["vector"]`가 "순수 벡터 baseline"이라는 docstring의
        약속이 거짓이었고(실제 steps: document_search#semantic + lexical_search#semantic +
        vector_search), 그 값으로 잰 A/B의 벡터 단독 arm이 전부 무효였다. 실행 순서는 그때와
        같다(vector의 ANN 조회 → arm의 제목/본문 조회 → 둘 다 빈손일 때만 `_term_fallback`) -
        `_claim_anchor`가 "먼저 잡은 anchor를 덮어쓰지 않는다"에 기대고 있어 순서를 바꾸면
        지표가 움직인다.

        원래는 `if not hits`, 즉 vector가 0히트일 때만 도는 fallback이었다. 임베딩이 0행인
        동안은 vector가 항상 실패해서 이 경로가 **항상** 돌았고, v0 골든셋 ndcg@10 0.709 →
        0.865는 사실상 이 어휘 경로가 만든 점수다. 임베딩을 채우자 ANN이 관련도와 무관하게
        늘 `limit`개를 돌려주면서 조건이 성립하지 않아 경로가 통째로 꺼졌고, 지표가 0.865 →
        0.804로 내려갔다(run 9ad2bba2). `steps`를 열어 보니 `v0-navigational-004`
        ("Dev Handover README") 같은 질의가 vector 단독 30히트만으로 랭킹되고 있었다.

        `suggest_strategies`가 `document_lookup`/`lexical`을 고르는 조건은
        `identifiers`/`quoted_phrases`/`document_reference`가 잡혔을 때뿐이라, 평범한 자연어
        질의는 그 그물에 안 걸린다. 반면 이 arm은 semantic_query 통째로 거는 더 넓은 그물이라
        둘은 대체재가 아니다 - 그래서 전략이 이미 돌았어도 이 arm을 끄지 않고, 대신 **소스
        이름을 나눠**(`#semantic`) RRF의 별도 투표자로 넣는다. `_term_fallback`이
        `lexical_search#<term>`으로 소스를 나눈 것과 같은 수법이고, 같은 소스에 append하면
        한 리스트 안에서 순위가 섞여 RRF가 망가지기 때문이다.

        두 조회는 `vector()`가 이번 실행에 벡터도 있으면 미리(벡터 조회와 겹쳐서) 던져
        `self.arm_fetched`에 담아 둔다 - 그 경우 여기서는 받기만 한다. arm만 도는 실행
        (`strategies_override=["keyword_arm"]`)에서는 비어 있으므로 직접 던진다. 어느
        쪽이든 해석 순서는 제목 → 본문으로, 두 조회를 순차로 하던 때와 같다."""
        query = self.c.semantic_query
        if not query:
            return
        fetched = self.arm_fetched if self.arm_fetched is not None else await self._keyword_arm_fetch(query)
        title_hits = fetched.title
        # anchor 판정을 `document_lookup`과 공유한다(`_claim_anchor` docstring 참고). 이미
        # anchor가 잡혀 있으면 다시 판정하지 않는다 - 그건 `document_reference`/`identifiers`
        # 같은 명시적 신호에서 나온 더 강한 anchor라 semantic_query가 덮어쓰면 안 된다.
        anchor_reason = "already_anchored"
        if self.anchor is None:
            anchor_reason, _ranked = self._claim_anchor(title_hits)
        title_hits, _ = _narrow_to_anchor(title_hits, self.anchor)
        self.add_hits(SOURCE_DOCUMENT_SEARCH_SEMANTIC, title_hits)
        self.step(
            STRATEGY_DOCUMENT_LOOKUP,
            SOURCE_DOCUMENT_SEARCH_SEMANTIC,
            len(title_hits),
            query=query,
            arm="semantic_query",
            anchor=self.anchor,
            anchor_reason=anchor_reason,
        )
        body_hits, _ = _narrow_to_anchor(fetched.body, self.anchor)
        self.add_hits(SOURCE_LEXICAL_SEARCH_SEMANTIC, body_hits)
        self.step(STRATEGY_LEXICAL, SOURCE_LEXICAL_SEARCH_SEMANTIC, len(body_hits), query=query, arm="semantic_query")
        # 질의 문자열 통째로 뭔가 **강하게** 걸렸으면 그게 term 하나하나보다 강한 신호다 -
        # term 분해는 그 신호가 약할 때만 도는 구제 경로다. 항상 돌리면 "codex README"처럼
        # 제목이 정확히 맞은 질의에서 term별 히트가 정답을 밀어낸다(v0-navigational-002:
        # ndcg 1.000 → 0.356).
        #
        # 예전 게이트는 `not title_hits and not body_hits`, 즉 **0히트에만** 반응했다. 그래서
        # 약한 1히트가 게이트를 닫아 그 1건이 결과 전부가 됐다(v0-multihop-002: 슬랙 제목에
        # 발췌가 붙어 `document_search`가 1건을 물어오자 1.000 → 0.613. 기준선에서는 0히트라
        # fallback이 돌아 gmail 정답까지 데려왔다). 히트가 **있다/없다**가 아니라 **강하다/
        # 약하다**로 판정해야 하는 이유가 이것이다.
        #
        # 판정을 문서 수로, 임계를 2로 잡은 이유:
        # - 히트 수가 아니라 **문서 수**다. 한 문서의 청크 여러 개는 "여러 후보"가 아니다.
        # - 서로 다른 문서 1건은 애초에 랭킹이 아니다 - 비교 대상이 없어 RRF가 할 일이 없고,
        #   `lexical_search`의 3단 매칭에서 그 1건은 대개 마지막 `word_similarity` 티어로
        #   겨우 걸린 것이다. 반면 2건 이상이면 이미 순서가 있는 후보 집합이라, 거기에
        #   term별 소스 열 개를 얹으면 **약한 히트를 살리려다 강한 히트를 밀어낸다**
        #   (같은 실패 양식: `latest` 후처리, `feature/links-ranking`의 무게이트 링크 확장).
        #   그래서 임계는 일부러 최소값으로 둔다 - 구제 경로를 넓히는 상수가 아니다.
        # - anchor가 잡혔으면(제목 substring 완전 일치) 문서 수는 좁히기 때문에 1이 되지만
        #   그건 가장 강한 신호다. 그래서 anchor를 먼저 본다 - 이게 없으면
        #   v0-navigational-002가 그대로 회귀한다.
        arm_documents = len(
            {hit["document_id"] for hit in [*title_hits, *body_hits] if hit.get("document_id")}
        )
        if self.anchor is None and arm_documents < _ARM_STRONG_DOCUMENTS:
            await self._term_fallback()

    async def _term_fallback(self) -> None:
        """`keyword_arm`의 두 번째 단계 - 질의 문자열 하나가 아니라 `keyword_terms`가
        쪼갠 term마다 `lexical_search`를 부른다.

        왜 나눠 부르나: `lexical_search`의 매칭은 (전체 질의 substring → 모든 토큰 substring →
        `word_similarity`) 3단이라, 여러 어절짜리 자연어 질의는 전부 실패해 히트 0이 된다
        (v0 baseline에서 conversation_thread 0.250 / sequence 0.343의 실제 원인 - `steps`에
        `n_hits: 0`으로 남아 있다). term별 결과를 **소스를 나눠**(`lexical_search#<term>`) 넣는
        것이 핵심이다: `merge_and_rerank`의 RRF는 같은 히트가 여러 소스 리스트에 등장할수록
        점수를 더하므로, 소스를 나누면 "질의의 term을 더 많이 담은 문서"가 자동으로 위로 온다.
        한 리스트에 몰아넣으면 그 신호가 사라진다.

        term 하나짜리 질의는 건너뛴다 - 위의 전체 질의 조회와 같은 호출이 된다."""
        terms = keyword_terms(self.c, max_terms=_MAX_FALLBACK_TERMS)
        if len(terms) < 2:
            return
        limit = min(self.fetch_limit, _TERM_FETCH_LIMIT)
        hits_by_term: dict[str, list[dict[str, Any]]] = {}
        coverage: dict[str, set[str]] = {}
        observed_df: dict[str, int] = {}
        # term당 왕복 하나가 최대 열 번(`_MAX_FALLBACK_TERMS`)까지 나므로 이 arm이 이
        # 실행기의 최악 지연시간이다 - 전부 겹친다. gather가 입력 순서를 지키므로
        # `hits_by_term`의 키 순서(= 아래 `add_hits`가 RRF에 소스를 넣는 순서)도 그대로다.
        per_term = await asyncio.gather(
            *(
                lexical_search(term, self.store, project_id=self.project_id, limit=limit, since=self.since, until=self.until)
                for term in terms
            )
        )
        for term, raw_hits in zip(terms, per_term, strict=True):
            # df는 코퍼스의 성질이라 anchor 좁히기 **전**에 센다 - 좁힌 뒤 세면 anchor가 잡힌
            # 질의에서 모든 term의 df가 1로 붙어 변별력 판정이 무의미해진다.
            observed_df[term] = len({hit.get("document_id") for hit in raw_hits if hit.get("document_id")})
            hits, _ = _narrow_to_anchor(raw_hits, self.anchor)
            hits_by_term[term] = hits
            for hit in hits:
                document_id = hit.get("document_id")
                if document_id:
                    coverage.setdefault(document_id, set()).add(term)

        df = await self._term_document_frequency(terms, observed_df)
        required = _required_terms(terms, df)
        threshold = _coverage_threshold(len(terms))
        kept_documents = {
            d for d, matched in coverage.items() if len(matched) >= threshold and required <= matched
        }
        by_count = {d for d, matched in coverage.items() if len(matched) >= threshold}
        total = 0
        for term, hits in hits_by_term.items():
            kept = [hit for hit in hits if hit.get("document_id") in kept_documents]
            total += len(kept)
            if kept:
                self.add_hits(f"{SOURCE_LEXICAL_SEARCH}#{term}", kept)
        self.step(
            STRATEGY_LEXICAL,
            SOURCE_LEXICAL_SEARCH,
            total,
            terms=terms,
            per_term={term: len(hits) for term, hits in hits_by_term.items()},
            document_frequency=df,
            required_terms=sorted(required),
            limit=limit,
            coverage_threshold=threshold,
            kept_documents=len(kept_documents),
            dropped_documents=len(coverage) - len(kept_documents),
            dropped_by_rare_gate=len(by_count - kept_documents),
            fallback_from=STRATEGY_KEYWORD_ARM,
            mode="per_term",
        )

    async def _term_document_frequency(self, terms: list[str], observed: dict[str, int]) -> dict[str, int]:
        """term별 df - `_term_document_frequency_sql`(정확, 추가 질의 1회) 결과와 실측
        히트에서 센 df의 `max`. `max`인 이유는 두 값이 재는 게 다르기 때문이다: SQL은
        substring 티어만 세므로 fuzzy 티어로만 걸린 term이 0으로 나오고, 실측은 `LIMIT`에
        잘려 흔한 term을 과소평가한다. 각자 **과소평가만** 하므로 큰 쪽이 진실에 가깝고,
        무엇보다 "히트가 있는데 df=0이라 전부 기각" 같은 모순이 생기지 않는다.

        질의가 실패하면(권한/스키마) 실측 df로 조용히 물러난다 - 변별력 판정의 품질은
        떨어지지만 검색 자체가 죽는 것보다 낫다."""
        if settings.term_rare_df_max < 0:
            return dict(observed)
        df = dict(observed)
        try:
            sql = _term_document_frequency_sql(
                terms, project_id=self.project_id, since=self.since, until=self.until
            )
            rows, _columns = await self.store.execute_readonly_query(sql, len(terms))
            for index, count in rows:
                term = terms[int(index)]
                df[term] = max(int(count), observed.get(term, 0))
        except Exception as exc:  # noqa: BLE001 - df는 보조 신호다, 실패해도 검색은 계속
            logger.warning("term document frequency query failed, using observed df: %s", exc)
        return df

    async def comparison(self) -> None:
        collected: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        targets = list(self.c.comparison_targets)
        ranges = [parse_temporal_expression(target, now=self.now) for target in targets]
        # 대상별 제목 조회를 한꺼번에 던진다. 본문 폴백은 제목 조회가 빈손인 대상만 도는
        # 2단계라 겹칠 수 없으므로 그대로 순차로 둔다(대개 0~1개다). `pinned` 순서는 아래
        # 루프가 대상 순서대로 돌기 때문에 그대로 유지된다 - 그 순서가 최종 랭킹의 고정
        # 순서(`merge()`)라서 바뀌면 안 된다.
        title_results = await asyncio.gather(
            *(
                document_search(
                    target,
                    self.store,
                    project_id=self.project_id,
                    limit=_COMPARISON_PER_TARGET,
                    since=(tr.start if tr else self.since),
                    until=(tr.end if tr else self.until),
                )
                for target, tr in zip(targets, ranges, strict=True)
            )
        )
        for target, time_range, hits in zip(targets, ranges, title_results, strict=True):
            since = time_range.start if time_range else self.since
            until = time_range.end if time_range else self.until
            via = SOURCE_DOCUMENT_SEARCH
            if not hits:
                # 제목에 없는 대상("6/22 미팅 아젠다 메일")은 본문 일치로 - 대상별 문서 하나는 반드시 찾아야
                # 비교가 성립한다.
                hits = await lexical_search(
                    target, self.store, project_id=self.project_id, limit=_COMPARISON_PER_TARGET, since=since, until=until
                )
                via = SOURCE_LEXICAL_SEARCH
            top = hits[0]["document_id"] if hits else None
            if top and top not in self.pinned:
                self.pinned.append(top)
            resolved.append({"target": target, "document_id": top, "via": via, "time_range": time_range.label if time_range else None})
            collected.extend(hits)
        self.add_hits(SOURCE_DOCUMENT_SEARCH, collected)
        self.step(STRATEGY_COMPARISON, SOURCE_DOCUMENT_SEARCH, len(collected), targets=resolved)

    async def multihop(self, provider: ChatProvider | None) -> None:
        if provider is None:
            self.step(STRATEGY_MULTIHOP, SOURCE_PLAN_SEARCH, 0, skipped="no_provider")
            return
        try:
            plan = await plan_and_search(
                self.c.query,
                provider,
                store=self.store,
                project_id=self.project_id,
                limit_per_subquery=self.limit,
                target_kind=self.grain,
            )
        except Exception as exc:  # noqa: BLE001 - planner 실패가 나머지 전략 결과를 버리게 하면 안 된다
            logger.warning("plan_and_search failed, continuing without it: %s", exc)
            self.step(STRATEGY_MULTIHOP, SOURCE_PLAN_SEARCH, 0, error=f"{type(exc).__name__}: {exc}")
            return
        hits: list[dict[str, Any]] = []
        dropped = 0
        for evidence in plan.get("evidence", []):
            for hit in evidence.get("results", []):
                if not hit.get("document_id"):
                    dropped += 1
                    continue
                hits.append({**hit, "subquery_id": evidence.get("subquery_id")})
        self.add_hits(SOURCE_PLAN_SEARCH, hits)
        self.step(
            STRATEGY_MULTIHOP,
            SOURCE_PLAN_SEARCH,
            len(hits),
            subqueries=[sq.get("query") for sq in plan.get("subqueries", [])],
            dropped_without_document_id=dropped,
        )

    # -- 병합 -----------------------------------------------------------------

    async def link_cohesion(self, merged: list[dict[str, Any]]) -> None:
        """`document_links`를 RRF의 **한 표**로 얹는다 - 병합 랭킹 상위(`_LINK_SEED_TOP_N`)와
        같은 대화에 묶여 있는 **이미 후보인** 문서를 조금 위로 올린다.

        왜 링크가 필요한가 / 무엇에는 필요 없는가:
        `same_channel_sequence`(145행)와 `thread_sequence`(58행)는 "채널/메일함 안에서 시간상
        인접한 두 건"이다. 그중 **시간 순서**는 링크의 고유 정보가 아니다 - 모든 히트가 이미
        `content_at`을 싣고 있고 그쪽이 전순서를 준다(`latest` 후처리가 그걸 쓴다). 링크가
        timestamp보다 더 아는 것은 **묶음** 하나뿐이다: 두 문서가 같은 채널/스레드에 속한다는
        사실은 타임스탬프만 봐서는 알 수 없다. 그래서 이 함수는 정렬이 아니라 **응집도**만
        신호로 쓴다.

        설계에서 일부러 하지 않은 것 - **링크로 새 문서를 데려오지 않는다.** 이웃이 후보
        집합(`merged`)에 이미 있어야 표를 받는다. `_Run.latest`가 밟았던 붕괴("관련도와 무관한
        문서를 위로 올리자 후보가 늘수록 나빠짐")가 링크 확장에서 재발하기 가장 쉬운 지점이
        정확히 여기다. v0 골든셋으로 실측한 근거도 있다: 랭킹에서 빠져 있는 정답 문서 중
        **상위 10건에서 링크 한 홉으로 닿는 것은 0건**이었다(v0-latest-003의 b2bf0036,
        v0-multihop-003의 54e9f0ab/b2bf0036 둘 다 document_links가 아예 없다). 즉 이 코퍼스에서
        링크 확장의 재현율 상한은 0이고, 얻을 수 있는 것은 **순서**뿐이다.

        또한 **고정(pin)이 아니라 표**다. anchor/comparison처럼 최상위로 끌어올리면 관련도
        랭킹을 덮어쓰게 된다. RRF 한 표(`1/(k+rank)`)는 "다른 arm 하나가 더 찾아준 것"과 같은
        크기라, 관련도가 낮은 이웃이 정답을 밀어내지 못한다.

        게이트는 질의 어휘가 아니라 **데이터**다: 후보 둘 이상이 실제로 한 대화에 묶여 있을
        때만 표가 나간다("순서/스레드" 같은 말이 질의에 있는지는 보지 않는다). 어휘 게이트를
        쓰지 않는 이유는 `_is_source_hint_only_filter`가 `source_type` 힌트를 못 믿는 이유와
        같다 - 질의 표면 어휘로 찍은 추측이고, 여기서는 그 추측 없이도 코퍼스가 직접
        "이 후보들은 한 대화다"라고 말해 준다."""
        if not _SEQUENCE_INTENT_RE.search(self.c.query):
            return
        ranked_documents, _ = _dedupe_documents(merged)
        if len(ranked_documents) < 2:
            return
        seeds = ranked_documents[:_LINK_SEED_TOP_N]
        seed_rank = {document_id: rank for rank, document_id in enumerate(seeds)}
        candidates = set(ranked_documents)
        try:
            sql = _document_links_sql(seeds)
            rows, _columns = await self.store.execute_readonly_query(sql, _FILTER_MAX_ROWS)
        except Exception as exc:  # noqa: BLE001 - 보조 신호다, 실패해도 랭킹은 그대로 나간다
            logger.warning("document_links query failed, ranking without link cohesion: %s", exc)
            self.step(STRATEGY_LINK_COHESION, SOURCE_DOCUMENT_LINKS, 0, error=f"{type(exc).__name__}: {exc}")
            return
        # 문서 -> 그 문서가 붙어 있는 씨앗들의 순위. 자기 자신(씨앗이 씨앗에 붙은 경우)도
        # 센다 - "상위 두 건이 같은 채널"이라는 사실 자체가 그 채널을 지지하는 신호다.
        neighbors: dict[str, set[int]] = {}
        types_seen: dict[str, set[str]] = {}
        for from_id, to_id, link_type in rows:
            for document_id, other in ((from_id, to_id), (to_id, from_id)):
                if document_id not in candidates or other not in seed_rank or other == document_id:
                    continue
                neighbors.setdefault(document_id, set()).add(seed_rank[other])
                types_seen.setdefault(document_id, set()).add(str(link_type))
        if not neighbors:
            self.step(STRATEGY_LINK_COHESION, SOURCE_DOCUMENT_LINKS, 0, sql=sql, seeds=len(seeds), reason="no_cluster")
            return
        merged_rank = {document_id: rank for rank, document_id in enumerate(ranked_documents)}
        # 정렬 키는 전부 결정론적이다: 붙은 씨앗 수(많을수록 대화의 중심) → 가장 높은 씨앗의
        # 순위 → 원래 병합 순위. 마지막 항이 tie를 완전히 깨므로 같은 입력이면 같은 순서다.
        voted = sorted(
            neighbors,
            key=lambda d: (-len(neighbors[d]), min(neighbors[d]), merged_rank[d]),
        )
        boost = {document_id: 1.0 / (DEFAULT_RRF_K + rank) for rank, document_id in enumerate(voted, start=1)}
        for hit in merged:
            delta = boost.get(hit["document_id"])
            if delta is not None:
                hit["rerank_score"] = float(hit.get("rerank_score") or 0.0) + delta
                hit.setdefault("retrieval_sources", []).append(SOURCE_DOCUMENT_LINKS)
        before = list(ranked_documents)
        # `sort`는 안정 정렬이라 점수가 같은 히트끼리는 병합 순서를 그대로 지킨다.
        merged.sort(key=lambda hit: -float(hit.get("rerank_score") or 0.0))
        after, _ = _dedupe_documents(merged)
        self.step(
            STRATEGY_LINK_COHESION,
            SOURCE_DOCUMENT_LINKS,
            len(voted),
            sql=sql,
            seeds=len(seeds),
            link_types=sorted({t for types in types_seen.values() for t in types}),
            promoted=[
                {"document_id": d, "from": before.index(d) + 1, "to": after.index(d) + 1, "seeds": len(neighbors[d])}
                for d in voted
                if after.index(d) < before.index(d)
            ],
        )

    async def merge(self) -> list[dict[str, Any]]:
        """관련도 랭킹(`merged`)에 메타데이터 필터(`filter_ids`)를 겹쳐 최종 순위를 만든다.

        **필터는 관련도 랭킹을 좁히는 도구지 대체하는 도구가 아니다.** 아래 분기들은 전부 그
        원칙과, "이 필터를 얼마나 믿을 수 있나"(`_is_source_hint_only_filter`)의 조합이다."""
        candidate_sources = {k: _with_document_id(v) for k, v in self.results_by_source.items() if v}
        merged = merge_and_rerank(candidate_sources)
        # 링크 응집도는 **필터/anchor 앞**이다. 이 단계는 후보 집합을 바꾸지 않고 순서만
        # 바꾸므로 아래 교집합(`filter_ids`)의 결과 집합은 그대로고, anchor 고정은 어차피
        # 순서와 무관하게 맨 앞으로 옮기는 partition이라 영향을 받지 않는다.
        if merged:
            await self.link_cohesion(merged)

        filter_note: str | None = None
        if self.filter_ids is not None:
            if not self.filter_ids:
                filter_note = "filter_empty_ignored"
            elif not candidate_sources:
                # **주의: 이 분기는 오늘 도달 불가능하다.** `keyword_arm`이 상시화된 뒤로
                # semantic_query가 있는 질의는 제목/본문 조회가 최소 한 건은 물어오므로
                # `candidate_sources`가 비지 않는다 - v0 76질의에서 metadata_filter가 돈 30건 중
                # `filter_only`는 **0건**이다(예전 baseline 5건).
                #
                # 그런데도 지우지 않고 남겨 두는 이유: 이 분기의 **의도**("질의가 제약 말고는
                # 아무 내용이 없으니 필터 집합이 곧 답이다")는 지금 아래 두 분기가 이미 커버하고
                # 있고, 그 커버가 v0에서 손실을 내지 않는다. 실측(run ff9c6393): 구조적 술어가
                # 있는 8건(temporal 3, relative_temporal 3, metadata_filter-001, aggregation-003)은
                # 전부 `intersected`/`no_overlap_fallback_to_filter`로 가서 **ndcg@10 1.000**이다.
                # 즉 교집합을 무조건 하는 것이 오늘 코퍼스에서 아무것도 깨뜨리지 않는다.
                # 반대로 조건을 "관련도 arm에 독립 신호가 없다" 같은 것으로 되살리면, 어휘 arm이
                # 정답을 제대로 찾은 질의에서까지 랭킹을 버리고 최신순 필터 집합을 답으로 내는
                # 회귀 위험만 생긴다(이득 0, 위험 > 0).
                #
                # 남기는 값어치는 안전망이다: 임베딩이 없거나 `strategies_override`로 arm을
                # 끈 실행에서는 여전히 도달하고, 그때 답이 통째로 비는 것을 막는다.
                # 판단 기준 전체는 wiki/search-architecture.md "metadata_filter를 언제 믿는가".
                #
                # 필터 단독 - 최신순 정렬된 필터 집합 자체가 결과. 여기에는 "관련도 랭킹을 버렸다"는
                # 문제가 없다: 버릴 관련도 랭킹 자체가 없다(document/lexical/vector 전부 0건).
                # "작년 3분기에 김민수가 보낸 메일" 같은 순수 메타데이터 질의는 검색어로 쓸 내용어가
                # 없어 정상적으로 이 경로를 탄다. 여기서까지 필터를 무시하면 답이 통째로 빈다 -
                # 아래 source_hint 분기와 정반대 상황이라 같이 손댈 이유가 없다.
                merged = merge_and_rerank({SOURCE_SQL_SEARCH: self.filter_hits})
                filter_note = "filter_only"
            elif _is_source_hint_only_filter(self.c):
                # **어휘 힌트뿐인 필터는 하드 필터로 쓰지 않는다.** `source_type IN (...)` 하나뿐인
                # WHERE는 `SOURCE_TYPE_MARKERS` 정규식이 질의 표면 어휘만 보고 찍은 추측이고, 그
                # 어휘 → source_type 매핑은 방향이 자주 반대다(코퍼스 실측은 그 상수 주석에).
                # 관련도 쪽 arm이 후보를 물어왔다면 그쪽이 훨씬 더 믿을 만하다.
                #
                # 원래 버그: 교집합이 0이면 관련도 랭킹을 통째로 버리고 필터 집합을 최신순으로
                # 내보냈다. 그래서 "애자일소다 Google Drive 폴더 공유 시 부여할 수 있는 역할"의
                # 정답(Drive 공유요청 **알림 메일**, source_type=gmail)을 `document_search`가
                # score 1.0으로 1위에 잡아둬도 통째로 버려졌고, 서로 다른 두 질의가 **완전히 같은
                # 7건을 같은 순서로** 돌려줬다 - 질의를 사실상 무시한 것이다.
                #
                # 교집합 0만 고치는 것으로는 부족했다. 교집합이 **우연히 조금 생기는** 경우가
                # 남는데(위 질의에서 벡터 arm이 드라이브 파일 4건을 같이 물어오면 `intersected:52->4`)
                # 그때도 1위 정답이 그대로 잘려나간다. 그래서 교집합을 보기 전에 끊는다.
                #
                # 잃는 것: 어휘 힌트가 맞았을 때의 정밀도. v0에서는 손실이 관측되지 않았다
                # (hit@1 0.843→0.857, ndcg@10 0.893→0.897, 나머지 지표 동일).
                #
                # 버린 대안:
                # - 하드 필터 대신 재정렬(교집합 먼저, 나머지를 뒤에): 위 질의에서 1위 정답이
                #   드라이브 파일 4건 뒤 5위로 밀린다. 잘려나가지만 않을 뿐 마찬가지로 나쁘다.
                # - `google_drive` 마커를 ['google_drive','gmail']로 확장: 이 두 질의만 낫고
                #   슬랙/노션에서 같은 모양으로 재발한다. 진짜 드라이브 질의의 필터도 넓어진다.
                # - "LLM이 확정한 제약만 하드 필터": `source_types`는 애초에 LLM 보강 대상이
                #   아니라(`constraints.LLM_ENRICHABLE_FIELDS`) provenance로는 갈리지 않는다.
                #   그래서 술어의 종류로 가른다 - `_is_source_hint_only_filter` 참고.
                filter_note = "source_hint_not_applied"
            else:
                kept = [hit for hit in merged if hit["document_id"] in self.filter_ids]
                if kept:
                    filter_note = f"intersected:{len(merged)}->{len(kept)}"
                    merged = kept
                else:
                    # 여기까지 왔으면 필터에 작성자/참여자/시간/제목 종류 같은 **구조적 술어**가
                    # 들어 있다. 그런 필터는 교집합 0에서도 믿는 쪽이 맞다: 이런 질의는 조건을
                    # 걷어내고 나면 검색어가 거의 남지 않아("nonghyup.com 도메인 발신자가 보낸
                    # 메일" → semantic_query "보낸", "최근 3일 안에 농협 측에서 온 메일" →
                    # "안에 농협 측 온") 관련도 arm 쪽이 오히려 쓰레기를 물어온다. 실제로 이
                    # 분기까지 같이 없앴더니 v0에서 metadata_filter ndcg 0.817→0.543,
                    # relative_temporal 1.000→0.578으로 무너졌다(v0-metadata_filter-001과
                    # v0-relative_temporal-001이 1.000→0.000).
                    merged = merge_and_rerank({SOURCE_SQL_SEARCH: self.filter_hits})
                    filter_note = "no_overlap_fallback_to_filter"
        if self.filter_step is not None:
            self.filter_step.detail["applied"] = filter_note

        # anchor → comparison 대상 순으로 최상위 고정. 순서를 지키는 partition이라 나머지 랭킹은 그대로.
        for document_id in reversed([*([self.anchor] if self.anchor else []), *self.pinned]):
            front = [hit for hit in merged if hit["document_id"] == document_id]
            merged = front + [hit for hit in merged if hit["document_id"] != document_id]
        return merged

    # -- 후처리 ---------------------------------------------------------------

    async def latest(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 시각 정렬은 **관련도 상위 pool 안에서만** 한다. 후보 전체를 시각으로 정렬하면 병합
        # 랭킹이 통째로 버려져 "질의와 상관없이 가장 최근 문서"가 1위가 된다 - 후보 수가 늘수록
        # 나빠지는 종류의 버그다(v0-latest-001/002: 후보가 2→96, 6→119이 되자 정답이 무관한
        # 최근 슬랙 메시지 뒤로 밀려 ndcg 0.000). pool 밖은 관련도 순 그대로 뒤에 붙인다.
        ranked_documents, _ = _dedupe_documents(hits)
        pool_ids = set(ranked_documents[: max(1, settings.latest_relevance_pool)])
        pool = [hit for hit in hits if hit["document_id"] in pool_ids]
        tail = [hit for hit in hits if hit["document_id"] not in pool_ids]
        ordered = sorted(pool, key=lambda hit: _hit_time(hit), reverse=True) + tail
        document_ids, _ = _dedupe_documents(ordered)
        demoted: set[str] = set()
        families: dict[tuple[str | None, str], list[str]] = {}
        sql: str | None = None
        if document_ids:
            sql = (
                "SELECT d.document_id, d.project_id, d.metadata->>'version_family' AS family, "
                "d.metadata->>'version_label' AS label, (d.metadata->>'version_number')::float AS number, "
                "(d.metadata->>'is_final')::boolean AS is_final\nFROM documents d\n"
                "WHERE d.document_id IN (" + ", ".join(_sql_literal(i) for i in document_ids) + ")"
            )
            rows, _columns = await self.store.execute_readonly_query(sql, len(document_ids))
            meta = {row[0]: row for row in rows}
            time_of: dict[str, str] = {}
            for hit in ordered:
                time_of.setdefault(hit["document_id"], _hit_time(hit))
            for document_id in document_ids:
                row = meta.get(document_id)
                if row is not None and row[2]:
                    families.setdefault((row[1], row[2]), []).append(document_id)
            for members in families.values():
                if len(members) < 2:
                    continue
                # family 안에서는 버전 표기(초안 < 무표기 < vN < 수정본 < 최종)가 시각보다 우선 - 오래된
                # 파일을 나중에 다시 올린 경우 시각만 보면 구버전이 최신으로 보인다.
                newest = max(
                    members,
                    key=lambda i: (version_sort_key(meta[i][3], meta[i][4], meta[i][5]), time_of.get(i, "")),
                )
                demoted.update(m for m in members if m != newest)
        result = [hit for hit in ordered if hit["document_id"] not in demoted] + [
            hit for hit in ordered if hit["document_id"] in demoted
        ]
        self.step(
            STRATEGY_LATEST,
            SOURCE_VERSION_FAMILY,
            len(result),
            sql=sql,
            families={key[1]: members for key, members in families.items() if len(members) > 1},
            demoted=sorted(demoted),
            relevance_pool=len(pool_ids),
            outside_pool=len(ranked_documents) - len(pool_ids),
        )
        return result

    async def whole_document(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.anchor:
            targets = [self.anchor]
        else:
            document_ids, _ = _dedupe_documents(hits)
            targets = document_ids[:_WHOLE_DOCUMENT_TOP_N]
        chunk_hits: list[dict[str, Any]] = []
        for document_id in targets:
            page = await read_document(document_id, self.store, max_chars=_WHOLE_DOCUMENT_MAX_CHARS)
            if page is None:
                continue
            self.documents_read.append(document_id)
            source = page.get("source") or {}
            for chunk in page.get("chunks", []):
                text = chunk.get("text") or ""
                chunk_hits.append(
                    {
                        "target_kind": "chunk",
                        "knowledge_object_id": None,
                        "chunk_id": chunk.get("chunk_id"),
                        "document_id": document_id,
                        "sequence_index": chunk.get("sequence_index"),
                        "title": page.get("title"),
                        "source_type": source.get("source_type"),
                        "uri": source.get("uri"),
                        "modified_at": source.get("modified_at"),
                        "content_at": source.get("modified_at"),
                        "summary": text[:_HIT_SUMMARY_CHARS],
                        "score": 1.0,
                        "retrieval_sources": [SOURCE_READ_DOCUMENT],
                    }
                )
            self.step(
                STRATEGY_WHOLE_DOCUMENT,
                SOURCE_READ_DOCUMENT,
                len(page.get("chunks", [])),
                document_id=document_id,
                total_chunks=page.get("total_chunks"),
                truncated=page.get("truncated"),
            )
        read_chunk_ids = {hit["chunk_id"] for hit in chunk_hits if hit.get("chunk_id")}
        # 본문 청크를 문서 순서대로 앞에, 같은 청크를 가리키던 검색 히트는 중복이라 뺀다.
        return chunk_hits + [hit for hit in hits if hit.get("chunk_id") not in read_chunk_ids]

    async def count(self, document_ids: list[str]) -> None:
        via = "document_ids"
        sql: str | None = None
        # SQL COUNT는 **결과 목록이 곧 필터 집합인 경우에만** 쓴다 - 그때만 "몇 건인가"의 답이
        # 돌려준 목록과 일치한다. `source_hint_not_applied`가 여기 없는 이유가 그것이다: 그
        # 분기는 필터를 믿지 못해 랭킹에서 뺐는데 같은 필터로 센 숫자를 답으로 내면 목록과
        # 모순된다(`merge()` 참고).
        if self.filter_step is not None and self.filter_step.detail.get("applied") in (
            "filter_only",
            "no_overlap_fallback_to_filter",
        ):
            sql = _metadata_count_sql(self.filter_where)
            rows, _columns = await self.store.execute_readonly_query(sql, 1)
            self.predicted_count = int(rows[0][0]) if rows else 0
            via = "sql_count"
        else:
            self.predicted_count = len(document_ids)
        self.step(STRATEGY_COUNT, SOURCE_SQL_SEARCH, 0, predicted_count=self.predicted_count, via=via, sql=sql)


async def retrieve(
    query: str,
    *,
    store: PipelineStore,
    provider: ChatProvider | None = None,
    project_id: str | None = None,
    now: datetime | None = None,
    limit: int = 10,
    use_llm_constraints: bool = True,
    strategies_override: list[str] | None = None,
    grain: str | None = None,
) -> RetrievalResult:
    """질의 하나를 제약 추출 → 전략 선택 → 실행 → 병합까지 돌려 `RetrievalResult`로 돌려준다.

    - `provider=None`: 규칙 기반 제약 추출만, multihop(planner)은 건너뜀 - 평가 하네스의 기준선.
      `use_llm_constraints=False`면 provider가 있어도 제약 보강만 끈다(planner는 그대로).
    - `now`: 상대 시간 표현("최근 3일", "올해")의 기준. 골든셋의 `as_of`를 넣어 재현한다.
    - `limit`: 최종 `document_ids` 길이 상한(exhaustive/negative/count면 `EXHAUSTIVE_LIMIT`까지
      컷오프 없음). 소스별 조회는 `_OVERFETCH`배로 더 가져와 병합 뒤에도 limit개가 남게 한다.
    - `strategies_override`: `suggest_strategies` 대신 이 목록만 실행(A/B용). 이름은
      `constraints.py`의 `STRATEGY_*` 값이고, 목록에 없는 전략은 돌지 않는다 - 어휘 arm도
      `"keyword_arm"`으로 명시해야 돈다. 기본 라우팅은 `semantic_query`가 있으면 `vector`와
      `keyword_arm`을 **함께** 넣으므로, `["vector", "keyword_arm"]`이 기본 하이브리드와 같고
      `["vector"]`가 진짜 순수 벡터 baseline이다. v0 ndcg@10 실측(76질의, --no-llm):
      기본 라우팅 0.893 / `vector,keyword_arm` 0.736 / `keyword_arm` 단독 0.663 /
      `vector` 단독 0.477 - 어휘 arm 없는 벡터는 하이브리드의 절반 조금 넘는 수준이다.
    - `grain`: 벡터 조회가 볼 임베딩 grain(`target_kind`). None(기본)이면 청크 grain과 KO
      grain이 한 랭킹에서 경쟁한다. `"chunk"`/`"knowledge_object"`로 좁히면 그 grain만 본다 -
      `eval-ir --grain`이 쓰는 축이고, 사람이 치는 표기는 `normalize_grain()`이 옮긴다.
      어휘 arm(`document_search`/`lexical_search`)은 임베딩을 쓰지 않으므로 이 값과 무관하다:
      `--grain chunk`가 "청크만 본다"는 뜻이 아니라 "**벡터 arm이** 청크 임베딩만 본다"는 뜻이다.
    """
    started = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    if provider is not None and use_llm_constraints:
        constraints = await extract_constraints(query, provider, now=now)
    else:
        constraints = extract_constraints_rules(query, now=now)
    strategies = list(strategies_override) if strategies_override is not None else suggest_strategies(constraints)

    run = _Run(
        store=store,
        constraints=constraints,
        strategies=strategies,
        project_id=project_id,
        now=now,
        limit=limit,
        grain=grain,
    )

    # 후보 생성 - document_lookup이 먼저다(anchor가 뒤 전략을 좁힌다).
    if STRATEGY_DOCUMENT_LOOKUP in strategies:
        await run.document_lookup()
    if STRATEGY_LEXICAL in strategies:
        await run.lexical()
    if STRATEGY_METADATA_FILTER in strategies:
        await run.metadata_filter()
    if STRATEGY_VECTOR in strategies:
        await run.vector()
    # arm은 vector 다음이어야 한다 - `_claim_anchor`는 먼저 잡힌 anchor를 덮어쓰지 않고,
    # `_narrow_to_anchor`는 그 시점의 anchor로 좁힌다. 순서를 바꾸면 지표가 움직인다.
    if STRATEGY_KEYWORD_ARM in strategies:
        await run.keyword_arm()
    if STRATEGY_COMPARISON in strategies:
        await run.comparison()
    if STRATEGY_MULTIHOP in strategies:
        await run.multihop(provider)

    hits = await run.merge()

    # 후처리
    if STRATEGY_LATEST in strategies:
        hits = await run.latest(hits)
    if STRATEGY_WHOLE_DOCUMENT in strategies:
        hits = await run.whole_document(hits)

    document_ids, scores = _dedupe_documents(hits)
    if not run.wide and len(document_ids) > limit:
        keep = set(document_ids[:limit])
        hits = [hit for hit in hits if hit["document_id"] in keep]
        document_ids, scores = document_ids[:limit], scores[:limit]
    for hit in hits:
        hit.setdefault("score", 0.0)
        hit.setdefault("target_kind", "document")
    if STRATEGY_COUNT in strategies:
        await run.count(document_ids)

    return RetrievalResult(
        query=query,
        constraints=constraints.to_dict(),
        strategies=strategies,
        steps=run.steps,
        hits=hits,
        document_ids=document_ids,
        scores=scores,
        predicted_count=run.predicted_count,
        documents_read=run.documents_read,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
