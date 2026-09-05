"""wiki/query-types.md의 검색 유형 표 예시 질의를 파라미터화해 `search/constraints.py`의 규칙
단계가 기대한 제약/플래그/전략을 내는지 못 박는다. LLM 단계는 scripted provider로 병합 정책
(규칙이 확정한 필드는 LLM이 못 덮어씀, 파싱 실패 시 규칙 결과로 degrade)만 검증한다. DB 불필요."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider
from knowledge_forge.search.constraints import (
    ORIGIN_LLM,
    ORIGIN_RULE,
    QueryConstraints,
    extract_constraints,
    extract_constraints_rules,
    keyword_terms,
    merge_llm_enrichment,
    suggest_strategies,
)

# 2026-09-04 (금) 15:30 UTC - test_temporal.py와 같은 고정 기준 시각.
NOW = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@dataclass
class Case:
    query: str
    flags: set[str] = field(default_factory=set)
    authors: list[str] | None = None
    participants: list[str] | None = None
    project_hints: list[str] | None = None
    source_types: list[str] | None = None
    document_types: list[str] | None = None
    identifiers: list[str] | None = None
    quoted: list[str] | None = None
    document_reference: str | None = None
    time_range: tuple[datetime | None, datetime | None] | None = None
    time_label: str | None = None
    comparison_targets: list[str] | None = None
    semantic_query: str | None = None
    strategies_include: set[str] = field(default_factory=set)
    strategies_exclude: set[str] = field(default_factory=set)


ALL_FLAGS = {
    "wants_whole_document",
    "wants_summary",
    "wants_latest",
    "wants_exhaustive",
    "wants_count",
    "is_negative",
    "is_comparison",
    "is_multihop",
}

CASES = [
    # Navigational + Whole-document Summary
    Case(
        "2025_SH_업무_보고서 요약해줘",
        flags={"wants_summary", "wants_whole_document"},
        document_reference="2025_SH_업무_보고서",
        semantic_query="",
        strategies_include={"document_lookup", "whole_document"},
        strategies_exclude={"vector"},
    ),
    Case(
        "2025_SH_업무_보고서를 정리해줘",
        flags={"wants_summary", "wants_whole_document"},
        document_reference="2025_SH_업무_보고서",
    ),
    # Metadata Filter + Temporal
    Case(
        "김철수가 2025년 3월에 작성한 보고서",
        authors=["김철수"],
        document_types=["보고서"],
        time_range=(_utc(2025, 3, 1), _utc(2025, 4, 1)),
        strategies_include={"metadata_filter"},
        strategies_exclude={"document_lookup", "lexical"},
    ),
    Case(
        "김철수가 작성한 보고서 중 장애 관련",
        authors=["김철수"],
        document_types=["보고서"],
        semantic_query="보고서 장애",
        strategies_include={"metadata_filter", "vector"},
    ),
    Case("이지은이 쓴 회의록", authors=["이지은"], document_types=["회의록"]),
    Case("박영희가 보낸 메일 중 계약 관련", authors=["박영희"], source_types=["gmail"], semantic_query="계약"),
    Case("latest report from Alice", flags={"wants_latest"}, authors=["Alice"], document_types=["보고서"]),
    # Aggregation
    Case(
        "올해 프로젝트 몇 개 진행했어?",
        flags={"wants_count"},
        time_range=(_utc(2026, 1, 1), _utc(2027, 1, 1)),
        time_label="올해",
        strategies_include={"count", "metadata_filter"},
    ),
    Case("how many issues were reported in Q3", flags={"wants_count"}, time_label="2026년 Q3"),
    Case("작년에 처리한 장애 건수", flags={"wants_count"}, time_range=(_utc(2025, 1, 1), _utc(2026, 1, 1))),
    # Latest
    Case(
        "가장 최근 업무보고서",
        flags={"wants_latest"},
        document_types=["보고서"],
        semantic_query="업무보고서",
        strategies_include={"latest"},
    ),
    Case("최근 3개월 보고서 목록", flags={"wants_exhaustive"}, time_label="최근 3개월", strategies_include={"exhaustive"}),
    # Exhaustive / Set Query
    Case(
        "A 프로젝트 관련 문서 전부",
        flags={"wants_exhaustive"},
        project_hints=["A"],
        semantic_query="A 프로젝트",
        strategies_include={"exhaustive", "vector"},
    ),
    Case("모든 회의록 보여줘", flags={"wants_exhaustive"}, document_types=["회의록"]),
    # Negative
    Case(
        "A 프로젝트에서 언급되지 않은 기술",
        flags={"is_negative"},
        project_hints=["A"],
        semantic_query="A 프로젝트 기술",
        strategies_include={"negative", "exhaustive"},
    ),
    Case("계획서에 빠진 항목", flags={"is_negative"}, document_types=["계획서"]),
    # Cross-document Comparison
    Case(
        "2024년과 2025년 사업계획의 차이",
        flags={"is_comparison"},
        document_types=["계획서"],
        comparison_targets=["2024년 사업계획", "2025년 사업계획"],
        semantic_query="사업계획",
        strategies_include={"comparison"},
    ),
    Case("Redis vs Memcached 차이", flags={"is_comparison"}, comparison_targets=["Redis", "Memcached"]),
    Case("Redis vs Memcached", flags={"is_comparison"}, comparison_targets=["Redis", "Memcached"]),
    # Exact Match
    Case(
        "ERR_CONNECTION_RESET 관련 문서",
        identifiers=["ERR_CONNECTION_RESET"],
        semantic_query="",
        strategies_include={"lexical", "document_lookup"},
        strategies_exclude={"vector"},
    ),
    Case("PRJ-2025-001 이슈 상태", identifiers=["PRJ-2025-001"], semantic_query="이슈 상태"),
    Case(
        '"인증 방식을 OAuth로" 문구가 들어간 문서',
        quoted=["인증 방식을 OAuth로"],
        strategies_include={"lexical"},
        strategies_exclude={"document_lookup"},
    ),
    Case(
        "매출현황.xlsx 파일 내용 전체 보여줘",
        flags={"wants_whole_document"},
        identifiers=["매출현황.xlsx"],
        document_reference="매출현황.xlsx",
        strategies_include={"document_lookup", "whole_document"},
    ),
    # Temporal / Relative Temporal
    Case(
        "지난달 회의 결정사항",
        time_range=(_utc(2026, 8, 1), _utc(2026, 9, 1)),
        semantic_query="회의 결정사항",
        strategies_include={"metadata_filter", "vector"},
    ),
    Case("지난주 회의록 전문", flags={"wants_whole_document"}, document_types=["회의록"], time_label="지난주"),
    # Source-type hint / Conversation Thread
    Case("슬랙 개발 채널에서 어제 논의된 내용", source_types=["slack"], time_label="어제"),
    Case("김철수와의 회의에서 논의된 내용", participants=["김철수"]),
    # Multi-hop (추정)
    Case("그 프로젝트의 성공 이유", flags={"is_multihop"}, strategies_include={"multihop"}),
    Case(
        "OKF 프로젝트 담당자가 작성한 회의록",
        flags={"is_multihop"},
        authors=[],
        project_hints=["OKF"],
        document_types=["회의록"],
    ),
    # Ambiguous / 일반 QA - 아무 제약도 없어야 vector만 남는다
    Case(
        "인증 방식을 OAuth로 바꾼 결정",
        semantic_query="인증 방식 OAuth로 바꾼 결정",
        strategies_include={"vector"},
        strategies_exclude={"metadata_filter", "lexical", "document_lookup"},
    ),
    # 날짜형 토큰은 식별자가 아니다
    Case("2025-03-15 회의", identifiers=[], document_reference=None),
    # --- v0 골든셋 실측 회귀 케이스 ---------------------------------------------
    # 아래 세 질의는 v0 baseline에서 규칙 단계가 제약을 못 뽑아 검색이 헛돌았던 실물이다.
    # `v0-relative_temporal-003`: "일주일"을 못 읽어 SQL에 since가 안 붙었다(steps에서 since=null).
    Case(
        "최근 일주일 내 받은 Google Drive 공유 요청 메일",
        time_label="최근 1주",
        source_types=["gmail", "google_drive"],
        document_types=["메일"],
        strategies_include={"metadata_filter"},
        # "최근 <수량>"은 범위(temporal)지 정렬(latest) 신호가 아니다.
        strategies_exclude={"latest"},
    ),
    # `v0-aggregation-002`: "몇 통"을 못 읽어 count 전략이 아예 안 뽑혔다(predicted_count=None).
    Case(
        "Google Drive 공유 요청 메일은 총 몇 통인가?",
        flags={"wants_count"},
        strategies_include={"count", "metadata_filter"},
    ),
    # `v0-metadata_filter-001`: authors=["발신자"]로 0행이 나와 필터가 통째로 무시됐고
    # (applied="filter_empty_ignored"), 정작 도메인은 아무 필드에도 안 실렸다.
    Case(
        "nonghyup.com 도메인 발신자가 보낸 메일",
        authors=[],
        participants=["nonghyup.com"],
        source_types=["gmail"],
        document_types=["메일"],
        strategies_include={"metadata_filter"},
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.query for c in CASES])
def test_rule_extraction(case: Case):
    c = extract_constraints_rules(case.query, now=NOW)

    on_flags = {name for name in ALL_FLAGS if getattr(c, name)}
    assert on_flags == case.flags, f"flags {on_flags} != {case.flags}"

    if case.authors is not None:
        assert c.authors == case.authors
    if case.participants is not None:
        assert c.participants == case.participants
    if case.project_hints is not None:
        assert c.project_hints == case.project_hints
    if case.source_types is not None:
        assert c.source_types == case.source_types
    if case.document_types is not None:
        assert c.document_types == case.document_types
    if case.identifiers is not None:
        assert c.identifiers == case.identifiers
    if case.quoted is not None:
        assert c.quoted_phrases == case.quoted
    if case.document_reference is not None or case.identifiers == []:
        assert c.document_reference == case.document_reference
    if case.time_range is not None:
        assert c.time_range is not None
        assert (c.time_range.start, c.time_range.end) == case.time_range
    if case.time_label is not None:
        assert c.time_range is not None and c.time_range.label == case.time_label
    if case.comparison_targets is not None:
        assert c.comparison_targets == case.comparison_targets
    if case.semantic_query is not None:
        assert c.semantic_query == case.semantic_query

    strategies = suggest_strategies(c)
    assert case.strategies_include <= set(strategies), strategies
    assert not (case.strategies_exclude & set(strategies)), strategies


def test_rule_extraction_is_deterministic():
    a = extract_constraints_rules("김철수가 2025년 3월에 작성한 보고서 전부", now=NOW)
    b = extract_constraints_rules("김철수가 2025년 3월에 작성한 보고서 전부", now=NOW)
    assert a.to_dict() == b.to_dict()


def test_authors_and_project_hints_are_mirrored_into_entities():
    c = extract_constraints_rules("김철수가 작성한 OKF 프로젝트 보고서", now=NOW)
    assert c.authors == ["김철수"]
    assert c.project_hints == ["OKF"]
    assert set(c.entities) == {"김철수", "OKF"}


def test_comparison_with_two_distinct_years_leaves_time_range_open():
    """단일 time_range 필터는 비교 대상 한쪽을 잘라내므로 비워 둔다."""
    c = extract_constraints_rules("2024년과 2025년 사업계획의 차이", now=NOW)
    assert c.is_comparison and c.time_range is None


def test_comparison_with_one_temporal_expression_keeps_time_range():
    c = extract_constraints_rules("2025년 A 프로젝트와 B 프로젝트 비교", now=NOW)
    assert c.is_comparison
    assert c.time_range is not None and c.time_range.label == "2025년"


def test_origins_mark_rule_populated_fields():
    c = extract_constraints_rules("김철수가 2025년 3월에 작성한 보고서 전부", now=NOW)
    assert c.origins["authors"] == ORIGIN_RULE
    assert c.origins["time_range"] == ORIGIN_RULE
    assert c.origins["wants_exhaustive"] == ORIGIN_RULE
    assert "wants_count" not in c.origins
    assert "query" not in c.origins


# --- 역할 명사 / 연락처 (v0-metadata_filter-001) ---------------------------------


def test_role_nouns_are_not_parsed_as_author_names():
    """'발신자가 보낸'의 '발신자'는 사람 이름이 아니라 역할이다. v0-metadata_filter-001에서
    authors=["발신자"]가 실려 ILIKE '%발신자%'가 0행 → 필터가 통째로 무시됐다."""
    for query in (
        "발신자가 보낸 메일",
        "수신자가 받은 메일",
        "보낸사람이 작성한 문서",
        "받는사람이 공유한 자료",
        "참조가 보낸 메일",
    ):
        assert extract_constraints_rules(query, now=NOW).authors == [], query


def test_email_domain_becomes_a_participant_filter():
    """도메인은 sources.metadata->>'participants'("이름 <주소>" 배열의 JSON 텍스트)에
    ILIKE로 걸린다 - `search/retrieve.py::_metadata_filter_where`의 기존 계약."""
    c = extract_constraints_rules("nonghyup.com 도메인 발신자가 보낸 메일", now=NOW)
    assert c.participants == ["nonghyup.com"]
    assert "nonghyup.com" in c.entities
    assert "metadata_filter" in suggest_strategies(c)


def test_email_address_becomes_a_participant_filter():
    """주소 전체가 적힌 형태도 같은 필드로. 조사가 바로 붙어도 주소만 떼어낸다."""
    c = extract_constraints_rules("hong@nonghyup.com이 보낸 메일", now=NOW)
    assert c.participants == ["hong@nonghyup.com"]


def test_email_local_part_is_not_mistaken_for_an_identifier():
    """'hong_gil@...'의 로컬 파트는 `_CODE_ID_RE`가 코드형 식별자로 오인해 document_lookup을
    켠다. 주소를 먼저 걷어내야 한다."""
    c = extract_constraints_rules("hong_gil@nonghyup.co.kr 메일", now=NOW)
    assert c.participants == ["hong_gil@nonghyup.co.kr"]
    assert c.identifiers == []


def test_non_domain_dotted_tokens_are_left_alone():
    """회귀 방지: 파일명/버전 표기가 도메인으로 오인되면 안 된다."""
    c = extract_constraints_rules("매출현황.xlsx 파일 내용 전체 보여줘", now=NOW)
    assert c.participants == []
    assert c.identifiers == ["매출현황.xlsx"]


# --- 수 분류사 (v0-aggregation-002) ----------------------------------------------


def test_korean_counter_words_turn_on_wants_count():
    """COUNT_MARKERS가 "몇 개/건/명"만 알아서 COUNT_MARKERS가 "몇 통"을 놓쳐 v0-aggregation-002에서 count 전략이
    아예 안 뽑혔다. 한국어 수 분류사는 세는 대상마다 달라진다."""
    for query in (
        "Google Drive 공유 요청 메일은 총 몇 통인가?",
        "첨부 파일이 몇 장이야?",
        "보고서 몇 부 필요해?",
        "거래처가 몇 곳이지?",
    ):
        c = extract_constraints_rules(query, now=NOW)
        assert c.wants_count, query
        assert "count" in suggest_strategies(c), query


def test_ambiguous_counters_stay_off():
    """분류사를 넓히다 실제로 오탐이 났던 것들. "몇 년 전"/"몇 달 전"은 시점 표현이고,
    "총 몇 팀으로 구성되어 있나"(v0-factoid-004)는 코퍼스 개수가 아니라 문서 안에 적힌
    사실을 묻는 factoid다 - count 전략으로 새면 답이 숫자 하나로 바뀐다."""
    for query in (
        "몇 년 전에 있었던 장애",
        "몇 달 전 회의록",
        "NH MVP 프로젝트의 투입 인력은 총 몇 팀으로 구성되어 있나?",
    ):
        assert not extract_constraints_rules(query, now=NOW).wants_count, query


def test_full_hostname_is_an_exact_match_target_not_a_mail_domain():
    """회귀 방지: 서브도메인까지 붙은 호스트명/URL은 발신 도메인이 아니라 정확 일치 대상이다.
    v0-exact_match-002/003을 participants로 빨아들이면 lexical 후보가 통째로 사라진다."""
    for query in (
        "prepay-docs.168-107-13-173.sslip.io",
        "git.sparklingsoda.ai:8443/nhbank/apps/card-reco.git",
    ):
        c = extract_constraints_rules(query, now=NOW)
        assert c.participants == [], query
        assert "lexical" in suggest_strategies(c), query


# --- "최근 <한글 수사>" (v0-relative_temporal-003) --------------------------------


def test_recent_korean_word_quantity_is_a_time_range_not_a_latest_sort():
    """LATEST_MARKERS의 "최근"은 숫자가 뒤따를 때만 제외됐다. "최근 일주일"은 범위 표현이므로
    wants_latest를 켜면 안 되고(정렬로 상위 몇 건만 남으면 set query가 깨진다), time_range가
    실려야 한다."""
    c = extract_constraints_rules("최근 일주일 내 받은 Google Drive 공유 요청 메일", now=NOW)
    assert not c.wants_latest
    assert c.time_range is not None and c.time_range.label == "최근 1주"
    assert "latest" not in suggest_strategies(c)


def test_recent_korean_word_quantity_is_stripped_from_semantic_query():
    """시간 구절은 벡터 검색어에 남으면 노이즈다 - 숫자 표기와 같게 걷어내야 한다."""
    c = extract_constraints_rules("최근 한 달 장애 원인", now=NOW)
    assert c.semantic_query == "장애 원인"


def test_source_type_markers_never_leave_half_a_phrase_in_the_semantic_query():
    """마커 스트리핑은 구절을 통째로 처리한다 - 반쪽만 남기면 안 된다.

    실측 버그: `\\bg?drive\\b`가 "Google Drive"에서 "Drive"만 지워 semantic_query가
    "애자일소다 Google 폴더 공유 시 ..."라는 반쪽짜리 구절이 됐고, 남은 "Google"이 벡터 arm의
    검색어를 흐렸다. "Google Drive"는 코퍼스 본문(Drive 공유요청 알림 메일)의 내용어이기도 해서
    **통째로 남기는 쪽**을 골랐고(`_MARKER_KEEP_PATTERN`), "구글 메일"처럼 내용어가 아닌 쪽은
    통째로 지운다."""
    c = extract_constraints_rules("애자일소다 Google Drive 폴더 공유 역할", now=NOW)
    assert c.source_types == ["google_drive"]
    assert c.semantic_query == "애자일소다 Google Drive 폴더 공유 역할"

    # 맨 종류어는 그대로 걷어낸다 - "구글"만 남기지 않는다.
    c = extract_constraints_rules("구글 메일 계약 조건", now=NOW)
    assert c.source_types == ["gmail"]
    assert c.semantic_query == "계약 조건"


def test_bare_recent_still_means_latest():
    """회귀 방지: 수량 없는 "최근"은 여전히 정렬 신호다."""
    c = extract_constraints_rules("최근 업무보고서", now=NOW)
    assert c.wants_latest and c.time_range is None


def test_to_dict_serializes_time_range_as_iso_strings():
    d = extract_constraints_rules("지난달 회의 결정사항", now=NOW).to_dict()
    assert d["time_range"] == {"start": "2026-08-01T00:00:00+00:00", "end": "2026-09-01T00:00:00+00:00", "label": "지난달"}
    assert json.dumps(d, ensure_ascii=False)  # JSON-safe


# --- suggest_strategies -----------------------------------------------------


def test_suggest_strategies_orders_candidate_generation_before_postprocessing():
    c = QueryConstraints(
        query="q",
        identifiers=["ERR_X"],
        authors=["김철수"],
        semantic_query="장애 원인",
        wants_latest=True,
        wants_count=True,
    )
    assert suggest_strategies(c) == [
        "document_lookup",
        "lexical",
        "metadata_filter",
        "vector",
        "keyword_arm",
        "latest",
        "count",
    ]


def test_suggest_strategies_is_empty_for_empty_constraints():
    assert suggest_strategies(QueryConstraints(query="")) == []


def test_negative_implies_exhaustive_collection():
    c = QueryConstraints(query="q", is_negative=True, semantic_query="기술")
    assert suggest_strategies(c) == ["vector", "keyword_arm", "exhaustive", "negative"]


def test_vector_always_brings_the_keyword_arm_with_it():
    """둘은 하이브리드 한 쌍이라 라우팅에서 갈라지지 않는다. arm은 원래 이름 없이
    `retrieve.py::_Run.vector()` 안에서 무조건 이어 돌던 부작용이었고, 이름을 준 것은
    A/B에서 한쪽만 끄기 위해서지 기본 동작을 바꾸려는 게 아니다 - 이 테스트가 그
    "기본 동작은 그대로"를 못 박는다(전략 이름이 하나 늘었다고 라우팅이 달라지면
    v0 ndcg@10 0.893이 재현되지 않는다)."""
    for c in (
        QueryConstraints(query="q", semantic_query="장애 원인"),
        QueryConstraints(query="q", identifiers=["ERR_X"], semantic_query="장애 원인"),
    ):
        strategies = suggest_strategies(c)
        assert strategies.index("keyword_arm") == strategies.index("vector") + 1
    # semantic_query가 없으면 둘 다 없다 - arm 혼자 도는 라우팅은 없다.
    assert suggest_strategies(QueryConstraints(query="q", identifiers=["ERR_X"])) == ["document_lookup", "lexical"]


# --- LLM 보강 단계 ------------------------------------------------------------


class _ScriptedProvider(ChatProvider):
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[tuple[str, str]] = []

    async def send(self, messages, tools, system) -> AssistantTurn:
        self.calls.append((messages[0].content or "", system))
        return AssistantTurn(text=self._response_text, tool_calls=[], stop_reason="end_turn")


class _RaisingProvider(ChatProvider):
    async def send(self, messages, tools, system) -> AssistantTurn:
        raise RuntimeError("boom")


async def test_extract_constraints_without_provider_equals_rules():
    query = "김철수가 2025년 3월에 작성한 보고서"
    assert (await extract_constraints(query, None, now=NOW)).to_dict() == extract_constraints_rules(query, now=NOW).to_dict()


async def test_llm_enriches_only_allowed_fields_and_cannot_override_rule_facts():
    payload = {
        "entities": ["김철수", "OKF"],
        "authors": ["김철수", "박영희"],
        "document_reference": None,
        "document_types": ["보고서", "회의록", "존재하지않는라벨"],
        "semantic_query": "장애 원인 분석 보고서",
        "is_multihop": True,
        "comparison_targets": [],
        # 아래는 전부 무시돼야 한다
        "time_range": {"start": "2020-01-01T00:00:00+00:00", "end": None, "label": "조작"},
        "identifiers": ["FAKE-1"],
        "wants_count": True,
        "is_negative": True,
    }
    provider = _ScriptedProvider(json.dumps(payload, ensure_ascii=False))

    c = await extract_constraints("김철수가 2025년 3월에 작성한 보고서 중 장애 관련", provider, now=NOW)

    assert c.authors == ["김철수", "박영희"]
    assert c.entities == ["김철수", "OKF", "박영희"]
    assert c.document_types == ["보고서", "회의록"]
    assert c.semantic_query == "장애 원인 분석 보고서"
    assert c.is_multihop is True
    # 규칙이 확정한 것은 그대로
    assert c.time_range is not None and (c.time_range.start, c.time_range.end) == (_utc(2025, 3, 1), _utc(2025, 4, 1))
    assert c.identifiers == []
    assert c.wants_count is False and c.is_negative is False
    assert c.origins["authors"] == ORIGIN_LLM
    assert c.origins["semantic_query"] == ORIGIN_LLM
    assert c.origins["is_multihop"] == ORIGIN_LLM
    assert c.origins["time_range"] == ORIGIN_RULE
    # 프롬프트에 규칙 결과가 실려 갔는지
    user_message, system = provider.calls[0]
    assert "김철수가 2025년 3월에 작성한 보고서 중 장애 관련" in user_message
    assert '"authors": ["김철수"]' in user_message
    assert "JSON" in system


async def test_llm_fills_document_reference_only_when_rules_found_none():
    provider = _ScriptedProvider(json.dumps({"document_reference": "SH 업무 보고서"}, ensure_ascii=False))
    c = await extract_constraints("SH 업무 보고서 요약해줘", provider, now=NOW)
    assert c.document_reference == "SH 업무 보고서"
    assert c.origins["document_reference"] == ORIGIN_LLM
    # 규칙이 summary를 켜 뒀으니 참조가 생기면 whole_document도 켜진다
    assert c.wants_whole_document is True

    c2 = merge_llm_enrichment(
        extract_constraints_rules("2025_SH_업무_보고서 요약해줘", now=NOW), {"document_reference": "다른 문서"}
    )
    assert c2.document_reference == "2025_SH_업무_보고서"
    assert c2.origins["document_reference"] == ORIGIN_RULE


async def test_llm_comparison_targets_replace_rule_split_only_when_rule_flagged_comparison():
    provider = _ScriptedProvider(json.dumps({"comparison_targets": ["2024년 사업계획서", "2025년 사업계획서"]}, ensure_ascii=False))
    c = await extract_constraints("2024년과 2025년 사업계획의 차이", provider, now=NOW)
    assert c.comparison_targets == ["2024년 사업계획서", "2025년 사업계획서"]
    assert c.origins["comparison_targets"] == ORIGIN_LLM

    c2 = await extract_constraints("2025년 사업계획", provider, now=NOW)
    assert c2.is_comparison is False and c2.comparison_targets == []


async def test_llm_cannot_unset_rule_multihop():
    provider = _ScriptedProvider(json.dumps({"is_multihop": False}))
    c = await extract_constraints("그 프로젝트의 성공 이유", provider, now=NOW)
    assert c.is_multihop is True


@pytest.mark.parametrize("raw", ["not json at all", "", "[1, 2, 3]", '```json\n{"semantic_query": ""}\n```'])
async def test_llm_bad_or_empty_response_degrades_to_rules(raw: str):
    query = "가장 최근 업무보고서"
    c = await extract_constraints(query, _ScriptedProvider(raw), now=NOW)
    assert c.to_dict() == extract_constraints_rules(query, now=NOW).to_dict()


async def test_llm_response_wrapped_in_code_fence_and_prose_is_parsed():
    raw = '물론입니다.\n```json\n{"semantic_query": "업무 보고서 최신본"}\n```\n이상입니다.'
    c = await extract_constraints("가장 최근 업무보고서", _ScriptedProvider(raw), now=NOW)
    assert c.semantic_query == "업무 보고서 최신본"


async def test_llm_exception_degrades_to_rules():
    query = "가장 최근 업무보고서"
    c = await extract_constraints(query, _RaisingProvider(), now=NOW)
    assert c.to_dict() == extract_constraints_rules(query, now=NOW).to_dict()


# --- keyword_terms -----------------------------------------------------------------


def test_keyword_terms_splits_a_natural_language_query():
    c = extract_constraints_rules("농협 MVP 투입 인력 명함 제작 논의 스레드", now=NOW)
    assert keyword_terms(c) == ["농협", "MVP", "투입", "인력", "명함", "제작", "논의"]


def test_keyword_terms_puts_literals_first_and_dedupes():
    """식별자/따옴표 구절이 semantic_query 토큰보다 정밀하므로 앞에 온다 - max_terms로 자를 때
    살아남아야 하는 쪽이다."""
    c = extract_constraints_rules('ERR_CONNECTION_RESET "재시도 정책" 문서에서 재시도 관련 부분', now=NOW)
    terms = keyword_terms(c)
    assert terms[0] == "ERR_CONNECTION_RESET"
    assert "재시도 정책" in terms
    assert len(terms) == len(set(terms))


def test_keyword_terms_drops_single_character_and_generic_tokens():
    c = extract_constraints_rules("배포 관련 내용 정보 A", now=NOW)
    assert keyword_terms(c) == ["배포"]


def test_keyword_terms_keeps_two_syllable_nouns_that_end_in_a_particle_letter():
    """"논의"의 "의", "묻는"의 "는"을 조사로 떼면 1글자만 남아 term이 통째로 사라진다 -
    `_clean_semantic_query`와 같이 3음절 이상 한글에만 조사를 뗀다."""
    c = extract_constraints_rules("논의 회의 묻는 명함", now=NOW)
    assert keyword_terms(c) == ["논의", "회의", "묻는", "명함"]


def test_keyword_terms_respects_max_terms():
    c = extract_constraints_rules("가 나 다 라 마 인터뷰 일정 예상 안내 확정 전달 시간 변경", now=NOW)
    assert len(keyword_terms(c, max_terms=3)) == 3


def test_constraints_module_imports_standalone_without_agent_or_storage():
    """test_layering.py와 같은 이유(search/*는 agent/* 아래) + 평가 하네스가 DB/임베딩 없이 이
    모듈만 단독으로 쓸 수 있어야 하므로 agent/storage/planner가 딸려 오지 않는지 확인.
    `search/service.py`는 `search/__init__.py`가 패키지 차원에서 import하므로 검사 대상에서 뺀다."""
    import subprocess
    import sys

    code = (
        "import sys, knowledge_forge.search.constraints;"
        "bad = [m for m in sys.modules if m.startswith(('knowledge_forge.agent', 'knowledge_forge.storage', "
        "'knowledge_forge.search.planner'))];"
        "assert not bad, bad"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- 한국어 조사가 붙은 영어 마커 ------------------------------------------------


def test_latin_markers_survive_an_attached_korean_particle():
    """파이썬 `\\b`는 유니코드 인식이라 한글도 `\\w`다 - "Drive에서"의 `e`와 `에` 사이에는
    경계가 없어 `\\bg?drive\\b`가 통째로 실패했다. 한국어 질의에서 영어 고유명사 뒤에
    조사가 붙는 건 예외가 아니라 기본형이라, 이게 안 되면 `metadata_filter` 전략이
    아예 안 켜진다(실측: `v1-metadata_filter-001` ndcg 0.000)."""
    for query, expected in [
        ("Google Drive에서 수집된 엑셀 파일 전부", "google_drive"),
        ("Drive에 올라온 문서", "google_drive"),
        ("Slack에서 오간 대화", "slack"),
        ("Notion의 페이지", "notion"),
    ]:
        c = extract_constraints_rules(query)
        assert expected in c.source_types, (query, c.source_types)


def test_latin_boundary_still_refuses_a_latin_neighbour():
    """조사를 견디게 만든 변환이 낱말 안쪽까지 열어 주면 안 된다 - 매치를 늘리기만
    해야지 `install`이 `\\ball\\b`에 걸리기 시작하면 그건 다른 버그다."""
    assert extract_constraints_rules("driveway 관련 문서").source_types == []
    assert extract_constraints_rules("slackline 장비").source_types == []


def test_google_drive_is_kept_in_the_semantic_query_even_with_a_particle():
    """마커를 지우는 쪽과 남기는 쪽이 같은 경계 규칙을 써야 한다. 한쪽만 조사를
    견디면 "Google Drive에"에서 `Drive`만 지워지고 **"Google" 파편**이 남는다
    (실측: `v0-factoid-009` 1.000 -> 0.631)."""
    c = extract_constraints_rules("ProjectIntel 개발 인수인계 패키지가 Google Drive에 업로드된 일자는?")
    assert "Google Drive" in c.semantic_query, c.semantic_query


# --- 파일 종류(확장자 / MIME 타입) ----------------------------------------------


def test_file_type_words_become_extension_constraints():
    for query, expected in [
        ("Google Drive에서 수집된 엑셀(xlsx) 파일 전부", ".xlsx"),
        ("Google Drive에서 수집된 CSV 파일", ".csv"),
        ("파워포인트(pptx) 파일", ".pptx"),
        ("워드 파일 목록", ".docx"),
        ("PDF 문서 전부", ".pdf"),
        ("한글 파일 정리", ".hwpx"),
    ]:
        c = extract_constraints_rules(query)
        assert expected in c.file_extensions, (query, c.file_extensions)


def test_an_explicit_mime_type_is_taken_literally():
    """확장자로는 못 가르는 경우가 있다 - `.md`인데 mimeType이 text/plain인 문서가 따로
    있어서, "mimeType이 text/markdown"은 mimeType으로 잡아야 정답 6건이 나온다."""
    c = extract_constraints_rules("mimeType이 text/markdown으로 수집된 Drive 문서")
    assert c.mime_types == ["text/markdown"]
    assert c.file_extensions == []


def test_file_type_constraints_select_the_metadata_filter_strategy():
    from knowledge_forge.search.constraints import STRATEGY_METADATA_FILTER, suggest_strategies

    assert STRATEGY_METADATA_FILTER in suggest_strategies(extract_constraints_rules("CSV 파일 전부"))
