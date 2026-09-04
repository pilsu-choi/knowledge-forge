"""`search/retrieve.py::retrieve` - the retrieval plan executor.

The fake-store tests pin down each strategy's branching (anchor pinning,
metadata-filter intersection / empty-filter fallback, latest ordering +
version-family compression, exhaustive/count, whole_document reading,
comparison targets, multihop gating on `provider`, `strategies_override`)
without a database. `vector_search` is monkeypatched at the module level
because the real one needs an embedding provider. `TestAgainstPostgres`
seeds rows under a test-only project_id and deletes them on teardown (the
dev DB holds real data), skipped unless KF_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.search import retrieve as retrieve_module
from knowledge_forge.search.retrieve import EXHAUSTIVE_LIMIT, retrieve

DSN = os.environ.get("KF_TEST_DATABASE_URL")
_NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _doc_hit(document_id: str, score: float, content_at: str = "2026-08-01T00:00:00+00:00", title: str | None = None) -> dict:
    return {
        "target_kind": "document",
        "document_id": document_id,
        "title": title or document_id,
        "source_type": "google_drive",
        "uri": f"test://{document_id}",
        "modified_at": content_at,
        "content_at": content_at,
        "project_id": "p",
        "score": score,
    }


def _chunk_hit(document_id: str, chunk_id: str, score: float = 1.0, content_at: str = "2026-08-01T00:00:00+00:00") -> dict:
    return {
        "target_kind": "chunk",
        "knowledge_object_id": None,
        "chunk_id": chunk_id,
        "document_id": document_id,
        "summary": f"…{chunk_id}…",
        "content_at": content_at,
        "modified_at": content_at,
        "score": score,
    }


class _FakeStore:
    """Answers each store call from canned tables. `title_hits`/`chunk_hits`
    map a query string to its hit list (`None` key = default for any query).
    `execute_readonly_query` dispatches on the SQL shape the executor
    writes: COUNT(*) -> `count_value`, the version-family lookup ->
    `version_rows`, anything else -> `filter_rows`."""

    def __init__(
        self,
        *,
        title_hits: dict[str | None, list[dict]] | None = None,
        chunk_hits: dict[str | None, list[dict]] | None = None,
        filter_rows: list[tuple] | None = None,
        version_rows: list[tuple] | None = None,
        count_value: int = 0,
        documents: dict[str, tuple[Document, Source, list[Chunk]]] | None = None,
    ) -> None:
        self.title_hits = title_hits or {}
        self.chunk_hits = chunk_hits or {}
        self.filter_rows = filter_rows or []
        self.version_rows = version_rows or []
        self.count_value = count_value
        self.documents = documents or {}
        self.title_calls: list[tuple[str, int]] = []
        self.chunk_calls: list[tuple[str, int]] = []
        self.sql_calls: list[tuple[str, int]] = []

    @staticmethod
    def _pick(table: dict[str | None, list[dict]], query: str) -> list[dict]:
        hits = table.get(query, table.get(None, []))
        return [dict(h) for h in hits]

    async def search_documents_by_title(self, query, *, project_id=None, source_type=None, since=None, until=None, limit=10):
        self.title_calls.append((query, limit))
        return self._pick(self.title_hits, query)[:limit]

    async def search_chunks_lexical(self, query, *, project_id=None, since=None, until=None, limit=10):
        self.chunk_calls.append((query, limit))
        return self._pick(self.chunk_hits, query)[:limit]

    async def execute_readonly_query(self, sql: str, limit: int):
        self.sql_calls.append((sql, limit))
        if sql.startswith("SELECT COUNT(*)"):
            return [(self.count_value,)], ["n"]
        if "version_family" in sql:
            return list(self.version_rows), ["document_id", "project_id", "family", "label", "number", "is_final"]
        return list(self.filter_rows[:limit]), ["document_id", "title", "source_type", "uri", "modified_at", "content_at", "project_id"]

    async def get_document(self, document_id: str):
        entry = self.documents.get(document_id)
        return entry[0] if entry else None

    async def get_source(self, source_id: str):
        for _doc, source, _chunks in self.documents.values():
            if source.source_id == source_id:
                return source
        return None

    async def list_chunks_by_document(self, document_id: str, *, hierarchy_level=None):
        entry = self.documents.get(document_id)
        return sorted(entry[2], key=lambda c: c.sequence_index) if entry else []


def _patch_vector(monkeypatch, hits: list[dict] | Exception, calls: list | None = None):
    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, embedding_model=None, since=None, until=None, target_kind=None):
        if calls is not None:
            calls.append((query, limit, since, until))
        if isinstance(hits, Exception):
            raise hits
        return [dict(h) for h in hits]

    monkeypatch.setattr(retrieve_module, "vector_search", _fake_vector_search)


def _filter_row(document_id: str, content_at: datetime) -> tuple:
    return (document_id, document_id, "gmail", f"test://{document_id}", content_at, content_at, "p")


class _FakeProvider(ChatProvider):
    async def send(self, messages, tools, system) -> AssistantTurn:
        return AssistantTurn(text="{}", tool_calls=[], stop_reason="end_turn")


# --- document_lookup / anchor --------------------------------------------------


async def test_document_lookup_single_substring_match_becomes_anchor_and_narrows_lexical(monkeypatch):
    store = _FakeStore(
        title_hits={"report_v2.pdf": [_doc_hit("d1", 1.0), _doc_hit("d2", 0.6)]},
        chunk_hits={"report_v2.pdf": [_chunk_hit("d2", "c2"), _chunk_hit("d1", "c1")]},
    )
    _patch_vector(monkeypatch, [_chunk_hit("d3", "c3", 0.9), _chunk_hit("d1", "c1b", 0.8)])

    result = await retrieve("report_v2.pdf 에서 리스크 항목", store=store, now=_NOW)

    lookup = next(s for s in result.steps if s.strategy == "document_lookup")
    assert lookup.detail["anchor"] == "d1"
    assert lookup.detail["anchor_reason"] == "single_substring_match"
    assert result.document_ids[0] == "d1"
    lexical = next(s for s in result.steps if s.strategy == "lexical")
    assert lexical.detail["anchor_narrowing"] == "narrowed"
    assert all(h["document_id"] == "d1" for h in result.hits if h.get("chunk_id") in {"c1", "c2"})
    # vector hits were narrowed to the anchor too - d3 is gone
    assert "d3" not in result.document_ids
    assert "d2" in result.document_ids  # the ambiguous second title candidate still appears, below the anchor


async def test_document_lookup_two_substring_matches_is_ambiguous_and_lists_both(monkeypatch):
    store = _FakeStore(title_hits={"AGENTS.md": [_doc_hit("d1", 1.0), _doc_hit("d2", 1.0)]})
    _patch_vector(monkeypatch, [])

    result = await retrieve("AGENTS.md", store=store, now=_NOW)

    lookup = next(s for s in result.steps if s.strategy == "document_lookup")
    assert lookup.detail["anchor"] is None
    assert lookup.detail["anchor_reason"] == "ambiguous"
    assert result.document_ids == ["d1", "d2"]


# --- metadata_filter --------------------------------------------------------------


async def test_metadata_filter_intersects_other_sources_and_records_sql(monkeypatch):
    store = _FakeStore(filter_rows=[_filter_row("d2", datetime(2026, 8, 30, tzinfo=timezone.utc))])
    _patch_vector(monkeypatch, [_chunk_hit("d1", "c1", 0.9), _chunk_hit("d2", "c2", 0.8), _chunk_hit("d3", "c3", 0.7)])

    result = await retrieve("지난주 회의록에서 배포 일정", store=store, now=_NOW)

    assert "metadata_filter" in result.strategies
    step = next(s for s in result.steps if s.strategy == "metadata_filter")
    assert "d.title ILIKE '%회의록%'" in step.detail["sql"]
    assert "COALESCE(s.modified_at, s.collected_at) >=" in step.detail["sql"]
    assert step.detail["applied"].startswith("intersected:")
    assert result.document_ids == ["d2"]
    assert store.sql_calls[0][1] == retrieve_module._FILTER_MAX_ROWS


async def test_metadata_filter_with_no_rows_is_ignored(monkeypatch):
    store = _FakeStore(filter_rows=[])
    _patch_vector(monkeypatch, [_chunk_hit("d1", "c1", 0.9), _chunk_hit("d2", "c2", 0.8)])

    result = await retrieve("지난주 회의록에서 배포 일정", store=store, now=_NOW)

    step = next(s for s in result.steps if s.strategy == "metadata_filter")
    assert step.detail["applied"] == "filter_empty_ignored"
    assert result.document_ids == ["d1", "d2"]


async def test_metadata_filter_alone_returns_its_rows_newest_first(monkeypatch):
    rows = [
        _filter_row("new", datetime(2026, 9, 1, tzinfo=timezone.utc)),
        _filter_row("old", datetime(2026, 7, 1, tzinfo=timezone.utc)),
    ]
    store = _FakeStore(filter_rows=rows)
    _patch_vector(monkeypatch, [])

    result = await retrieve("김철수가 작성한 자료", store=store, now=_NOW)

    step = next(s for s in result.steps if s.strategy == "metadata_filter")
    assert "s.metadata->>'participants' ILIKE '%김철수%'" in step.detail["sql"]
    assert step.detail["applied"] == "filter_only"
    assert result.document_ids == ["new", "old"]
    assert result.scores == [1.0, 1.0]


async def test_sql_literal_escapes_quotes_and_like_metacharacters():
    assert retrieve_module._sql_literal("O'Brien") == "'O''Brien'"
    assert retrieve_module._like_literal("a_b%c") == "'%a\\_b\\%c%'"


# --- vector fallback --------------------------------------------------------------


async def test_keyword_arm_runs_alongside_vector_on_the_semantic_query(monkeypatch):
    """semantic_query를 제목/본문에도 거는 어휘 arm은 vector와 **나란히** 돈다 - vector가
    0히트일 때만 도는 fallback이 아니다. 원래는 fallback이었고, 임베딩이 0행인 동안에는
    vector가 늘 실패해서 항상 돌았다. 임베딩을 채우자 ANN이 언제나 `limit`개를 돌려주면서
    경로가 통째로 꺼졌고 v0 ndcg@10이 0.865 → 0.804로 내려갔다(retrieve.py::_Run.keyword_arm).
    소스 이름이 `#semantic`으로 갈리는 것까지 계약이다 - 같은 소스에 append하면 서로 다른
    질의의 순위가 한 리스트에 섞여 RRF가 망가진다."""
    store = _FakeStore(
        title_hits={"codex README": [_doc_hit("d1", 0.8)]},
        chunk_hits={"codex README": [_chunk_hit("d2", "c2", 0.6)]},
    )
    _patch_vector(monkeypatch, [])

    result = await retrieve("codex README", store=store, now=_NOW)

    assert result.strategies == ["vector", "keyword_arm"]
    arm = [s for s in result.steps if s.detail.get("arm") == "semantic_query"]
    assert [s.source for s in arm] == ["document_search#semantic", "lexical_search#semantic"]
    assert result.document_ids == ["d1", "d2"]


async def test_keyword_arm_still_runs_when_vector_returned_hits(monkeypatch):
    """위 테스트의 짝 - vector가 히트를 돌려줘도 어휘 arm이 꺼지지 않는다는 것이 이번
    수정의 요점이다. `suggest_strategies`는 identifiers/quoted_phrases가 잡혔을 때만
    document_lookup/lexical을 고르므로, 평범한 자연어 질의에서는 이 arm이 유일한 어휘
    신호다."""
    store = _FakeStore(
        title_hits={"codex README": [_doc_hit("d1", 0.8)]},
        chunk_hits={"codex README": [_chunk_hit("d2", "c2", 0.6)]},
    )
    _patch_vector(monkeypatch, [_chunk_hit("d3", "c3", 0.9)])

    result = await retrieve("codex README", store=store, now=_NOW)

    arm = [s for s in result.steps if s.detail.get("arm") == "semantic_query"]
    assert [s.source for s in arm] == ["document_search#semantic", "lexical_search#semantic"]
    assert set(result.document_ids) == {"d1", "d2", "d3"}


async def test_term_fallback_is_skipped_when_the_whole_query_already_matched(monkeypatch):
    """질의 문자열 통째로 걸린 히트가 term 하나하나보다 강한 신호다 - term 분해는 그게 빈손일
    때만 도는 구제 경로. 항상 돌리면 제목이 정확히 맞은 질의를 term 히트가 밀어낸다
    (v0-navigational-002 "codex README": ndcg 1.000 → 0.356)."""
    store = _FakeStore(title_hits={"codex README": [_doc_hit("d1", 0.8)]})
    _patch_vector(monkeypatch, [])

    result = await retrieve("codex README", store=store, now=_NOW)

    assert not [s for s in result.steps if s.detail.get("mode") == "per_term"]
    assert [q for q, _limit in store.chunk_calls] == ["codex README"]


async def test_term_fallback_splits_the_query_and_ranks_by_term_coverage(monkeypatch):
    """자연어 질의는 전체 문자열로는 pg_trgm에 안 걸린다(히트 0) - term별로 나눠 찾고, 여러
    term이 겹친 문서가 RRF로 위에 온다. v0 baseline의 conversation_thread/sequence 실패 형태."""
    store = _FakeStore(
        chunk_hits={
            "명함": [_chunk_hit("thread", "c1", 0.5)],
            "제작": [_chunk_hit("thread", "c1", 0.5), _chunk_hit("other", "c9", 0.5)],
            "논의": [_chunk_hit("thread", "c1", 0.5)],
            "농협": [_chunk_hit("elsewhere", "c7", 0.5)],
        }
    )
    _patch_vector(monkeypatch, [])

    result = await retrieve("농협 명함 제작 논의", store=store, now=_NOW)

    step = next(s for s in result.steps if s.detail.get("mode") == "per_term")
    assert step.detail["terms"] == ["농협", "명함", "제작", "논의"]
    assert step.detail["per_term"] == {"농협": 1, "명함": 1, "제작": 2, "논의": 1}
    # thread만 term 3개(term 4개 × 2/3 → threshold 3) - 하나만 스친 elsewhere/other는 떨어진다.
    assert step.detail["coverage_threshold"] == 3
    assert (step.detail["kept_documents"], step.detail["dropped_documents"]) == (1, 2)
    assert result.document_ids == ["thread"]


async def test_term_coverage_threshold_keeps_recall_when_the_setting_is_off(monkeypatch):
    """`term_coverage_ratio`/`term_coverage_min`을 0으로 내리면 필터가 꺼지고 term 하나만
    겹친 문서도 후보로 남는다 - 재현율/정밀도 트레이드오프를 A/B로 재기 위한 노브."""
    store = _FakeStore(
        chunk_hits={
            "명함": [_chunk_hit("thread", "c1", 0.5)],
            "제작": [_chunk_hit("thread", "c1", 0.5), _chunk_hit("other", "c9", 0.5)],
            "논의": [_chunk_hit("thread", "c1", 0.5)],
            "농협": [_chunk_hit("elsewhere", "c7", 0.5)],
        }
    )
    _patch_vector(monkeypatch, [])
    monkeypatch.setattr(retrieve_module.settings, "term_coverage_ratio", 0.0)
    monkeypatch.setattr(retrieve_module.settings, "term_coverage_min", 0)

    result = await retrieve("농협 명함 제작 논의", store=store, now=_NOW)

    step = next(s for s in result.steps if s.detail.get("mode") == "per_term")
    assert step.detail["coverage_threshold"] == 1
    assert result.document_ids[0] == "thread"
    assert set(result.document_ids) == {"thread", "elsewhere", "other"}


def test_coverage_threshold_never_exceeds_the_term_count(monkeypatch):
    """하한(`term_coverage_min`)이 term 수보다 크면 아무것도 통과하지 못한다 - term 수로 자른다."""
    monkeypatch.setattr(retrieve_module.settings, "term_coverage_min", 5)
    assert retrieve_module._coverage_threshold(2) == 2
    assert retrieve_module._coverage_threshold(4) == 4
    assert retrieve_module._coverage_threshold(9) == 6  # 하한보다 비율(ceil(9 × 2/3))이 크면 비율


async def test_term_fallback_is_skipped_when_the_query_is_a_single_term(monkeypatch):
    """term이 하나면 위의 전체 질의 조회와 같은 호출이라 DB를 한 번 더 때릴 이유가 없다."""
    store = _FakeStore()
    _patch_vector(monkeypatch, [])

    result = await retrieve("배포", store=store, now=_NOW)

    assert not [s for s in result.steps if s.detail.get("mode") == "per_term"]
    assert [q for q, _limit in store.chunk_calls] == ["배포"]


async def test_term_fallback_caps_per_term_fetch_on_exhaustive_queries(monkeypatch):
    """exhaustive 질의는 최종 컷오프가 없다 - term당 조회까지 EXHAUSTIVE_LIMIT으로 열어 주면
    term 개수만큼의 노이즈가 그대로 답이 된다."""
    store = _FakeStore()
    _patch_vector(monkeypatch, [])

    await retrieve("배포 일정 전부 알려줘", store=store, now=_NOW)

    limits = dict(store.chunk_calls)
    assert limits.pop("배포 일정") == EXHAUSTIVE_LIMIT  # semantic_query ("전부 알려줘"는 군더더기)
    assert limits and set(limits.values()) == {retrieve_module._TERM_FETCH_LIMIT}


async def test_vector_search_error_is_recorded_not_raised(monkeypatch):
    store = _FakeStore()
    _patch_vector(monkeypatch, RuntimeError("no embedder"))

    result = await retrieve("배포 일정", store=store, now=_NOW)

    step = next(s for s in result.steps if s.strategy == "vector")
    assert step.n_hits == 0
    assert "no embedder" in step.detail["error"]
    assert result.document_ids == []


# --- latest -------------------------------------------------------------------------


async def test_latest_sorts_only_within_the_relevance_pool(monkeypatch):
    """`latest`가 후보 **전체**를 시각 정렬하면 병합 랭킹이 통째로 버려져 "질의와 상관없이 가장
    최근 문서"가 1위가 된다 - 후보 수가 늘수록 나빠지는 종류의 버그다(v0-latest-001/002가
    후보 2→96, 6→119이 되자 정답이 무관한 최근 슬랙 메시지 뒤로 밀려 ndcg 0.000). 관련도 상위
    pool 안에서만 시각 정렬하고, pool 밖은 관련도 순 그대로 뒤에 붙인다."""
    store = _FakeStore(
        chunk_hits={
            None: [
                _chunk_hit("relevant-old", "c1", 0.9, content_at="2026-08-01T00:00:00+00:00"),
                _chunk_hit("relevant-new", "c2", 0.8, content_at="2026-08-20T00:00:00+00:00"),
                _chunk_hit("noise-newest", "c3", 0.1, content_at="2026-09-03T00:00:00+00:00"),
            ]
        }
    )
    _patch_vector(monkeypatch, [])
    monkeypatch.setattr(retrieve_module.settings, "latest_relevance_pool", 2)

    result = await retrieve("가장 최근 배포 노트", store=store, now=_NOW)

    step = next(s for s in result.steps if s.strategy == "latest")
    assert (step.detail["relevance_pool"], step.detail["outside_pool"]) == (2, 1)
    # pool(관련도 1~2위) 안에서만 시각 정렬 - 더 최근이지만 관련 없는 noise-newest는 못 올라온다.
    assert result.document_ids == ["relevant-new", "relevant-old", "noise-newest"]



async def test_latest_orders_by_time_and_keeps_only_the_newest_of_a_version_family(monkeypatch):
    store = _FakeStore(
        version_rows=[
            ("d1", "p", None, None, None, None),
            ("d2", "p", "fam", None, None, False),
            ("d3", "p", "fam", "최종", None, True),
        ]
    )
    _patch_vector(
        monkeypatch,
        [
            _chunk_hit("d3", "c3", 0.9, "2026-07-01T00:00:00+00:00"),
            _chunk_hit("d1", "c1", 0.8, "2026-08-01T00:00:00+00:00"),
            _chunk_hit("d2", "c2", 0.7, "2026-09-01T00:00:00+00:00"),
        ],
    )

    result = await retrieve("배포 일정", store=store, now=_NOW, strategies_override=["vector", "latest"])

    # time order alone would give d2, d1, d3 - but d2/d3 share a family and d3 is the 최종 version
    assert result.document_ids == ["d1", "d3", "d2"]
    step = next(s for s in result.steps if s.strategy == "latest")
    assert step.detail["demoted"] == ["d2"]
    assert step.detail["families"] == {"fam": ["d2", "d3"]}


# --- exhaustive / count -------------------------------------------------------------


async def test_count_widens_limits_keeps_everything_and_predicts_the_document_count(monkeypatch):
    chunk_hits = [_chunk_hit(f"d{i}", f"c{i}") for i in range(5)]
    store = _FakeStore(chunk_hits={"caddy-drift": chunk_hits})
    _patch_vector(monkeypatch, [])

    result = await retrieve("caddy-drift 알림이 몇 건인가?", store=store, now=_NOW, limit=2)

    assert "count" in result.strategies
    assert store.chunk_calls[0][1] == EXHAUSTIVE_LIMIT
    assert len(result.document_ids) == 5  # no cut-off at limit=2
    assert result.predicted_count == 5
    count_step = next(s for s in result.steps if s.strategy == "count")
    assert count_step.detail["via"] == "document_ids"


async def test_count_over_a_filter_only_result_uses_sql_count(monkeypatch):
    store = _FakeStore(filter_rows=[_filter_row("d1", datetime(2026, 8, 5, tzinfo=timezone.utc))], count_value=23)
    _patch_vector(monkeypatch, [])

    result = await retrieve("2026년 8월 메일 몇 건", store=store, now=_NOW)

    assert result.predicted_count == 23
    count_step = next(s for s in result.steps if s.strategy == "count")
    assert count_step.detail["via"] == "sql_count"
    assert count_step.detail["sql"].startswith("SELECT COUNT(*)")


async def test_non_exhaustive_result_is_cut_to_limit_documents(monkeypatch):
    _patch_vector(monkeypatch, [_chunk_hit(f"d{i}", f"c{i}", 1.0 - i / 10) for i in range(6)])

    result = await retrieve("배포 일정", store=_FakeStore(), now=_NOW, limit=3)

    assert result.document_ids == ["d0", "d1", "d2"]
    assert len(result.scores) == 3
    assert {h["document_id"] for h in result.hits} == {"d0", "d1", "d2"}


# --- whole_document -----------------------------------------------------------------


def _seed_document(document_id: str, texts: list[str]) -> tuple[Document, Source, list[Chunk]]:
    source = Source(
        source_id=f"s-{document_id}",
        source_type=SourceType.LOCAL_FILE,
        uri=f"file:///{document_id}.md",
        content_hash="h",
        collected_at=_NOW,
        modified_at=_NOW,
        project_id="p",
    )
    document = Document(
        document_id=document_id,
        source=SourceRef.from_source(source),
        elements=[TextBlock(element_id="e1", text="x")],
        metadata=DocumentMetadata(title=f"{document_id} title"),
    )
    chunks = [
        Chunk(
            chunk_id=f"{document_id}-c{i}",
            document_id=document_id,
            source_id=source.source_id,
            hierarchy_level=HierarchyLevel.PARAGRAPH,
            sequence_index=i,
            text=text,
            token_count=len(text),
            element_ids=["e1"],
        )
        for i, text in enumerate(texts)
    ]
    return document, source, chunks


async def test_whole_document_reads_the_anchor_and_leads_with_its_chunks_in_order(monkeypatch):
    store = _FakeStore(
        title_hits={"report_v2.pdf": [_doc_hit("d1", 1.0)]},
        documents={"d1": _seed_document("d1", ["첫째", "둘째", "셋째"])},
    )
    _patch_vector(monkeypatch, [_chunk_hit("d9", "c9", 0.9)])

    result = await retrieve("report_v2.pdf 요약해줘", store=store, now=_NOW)

    assert "whole_document" in result.strategies
    assert result.documents_read == ["d1"]
    assert [h["chunk_id"] for h in result.hits[:3]] == ["d1-c0", "d1-c1", "d1-c2"]
    assert result.hits[0]["summary"] == "첫째"
    assert result.document_ids[0] == "d1"
    step = next(s for s in result.steps if s.strategy == "whole_document")
    assert step.source == "read_document" and step.n_hits == 3


async def test_whole_document_without_anchor_reads_the_top_two_documents(monkeypatch):
    store = _FakeStore(
        documents={"d1": _seed_document("d1", ["a"]), "d2": _seed_document("d2", ["b"]), "d3": _seed_document("d3", ["c"])}
    )
    _patch_vector(monkeypatch, [_chunk_hit("d1", "x1", 0.9), _chunk_hit("d2", "x2", 0.8), _chunk_hit("d3", "x3", 0.7)])

    result = await retrieve("보고서 전문 보여줘", store=store, now=_NOW, strategies_override=["vector", "whole_document"])

    assert result.documents_read == ["d1", "d2"]
    assert result.document_ids == ["d1", "d2", "d3"]


# --- comparison ---------------------------------------------------------------------


async def test_comparison_pins_the_top_document_of_every_target(monkeypatch):
    store = _FakeStore(
        title_hits={
            "2024년 사업계획": [_doc_hit("plan-2024", 0.9)],
            "2025년 사업계획": [_doc_hit("plan-2025", 0.9)],
        }
    )
    _patch_vector(monkeypatch, [_chunk_hit("other", "c0", 0.99)])

    result = await retrieve("2024년과 2025년 사업계획의 차이", store=store, now=_NOW)

    assert "comparison" in result.strategies
    step = next(s for s in result.steps if s.strategy == "comparison")
    assert [t["document_id"] for t in step.detail["targets"]] == ["plan-2024", "plan-2025"]
    assert result.document_ids[:2] == ["plan-2024", "plan-2025"]
    assert "other" in result.document_ids


async def test_comparison_falls_back_to_lexical_when_a_target_has_no_title_match(monkeypatch):
    store = _FakeStore(
        title_hits={"2025년 사업계획": [_doc_hit("plan-2025", 0.9)]},
        chunk_hits={"2024년 사업계획": [_chunk_hit("plan-2024", "c24")]},
    )
    _patch_vector(monkeypatch, [])

    result = await retrieve("2024년과 2025년 사업계획의 차이", store=store, now=_NOW)

    step = next(s for s in result.steps if s.strategy == "comparison")
    assert step.detail["targets"][0]["via"] == "lexical_search"
    assert set(result.document_ids[:2]) == {"plan-2024", "plan-2025"}


# --- multihop -----------------------------------------------------------------------


async def test_multihop_is_skipped_without_a_provider(monkeypatch):
    _patch_vector(monkeypatch, [])

    result = await retrieve("프로젝트 중에서 담당자가 작성한 문서", store=_FakeStore(), now=_NOW)

    assert "multihop" in result.strategies
    step = next(s for s in result.steps if s.strategy == "multihop")
    assert step.detail == {"skipped": "no_provider"}


async def test_multihop_joins_planner_evidence_and_drops_hits_without_document_id(monkeypatch):
    _patch_vector(monkeypatch, [])
    captured = {}

    async def _fake_plan_and_search(query, provider, *, store=None, project_id=None, limit_per_subquery=5):
        captured.update(query=query, provider=provider, limit=limit_per_subquery)
        return {
            "subqueries": [{"id": 1, "query": "q1", "depends_on": []}],
            "evidence": [
                {
                    "subquery_id": 1,
                    "subquery": "q1",
                    "results": [{"path": "p/okf.md", "snippet": "no document id"}, _chunk_hit("d7", "c7")],
                    "time_range": None,
                }
            ],
        }

    monkeypatch.setattr(retrieve_module, "plan_and_search", _fake_plan_and_search)
    provider = _FakeProvider()

    result = await retrieve(
        "프로젝트 중에서 담당자가 작성한 문서", store=_FakeStore(), provider=provider, now=_NOW, use_llm_constraints=False
    )

    assert captured["provider"] is provider
    step = next(s for s in result.steps if s.strategy == "multihop")
    assert step.n_hits == 1 and step.detail["dropped_without_document_id"] == 1
    assert result.document_ids == ["d7"]
    assert result.hits[0]["subquery_id"] == 1
    # use_llm_constraints=False: the rule-based constraints were used untouched
    assert result.constraints["origins"].get("is_multihop") == "rule"


# --- strategies_override / contract --------------------------------------------------


async def test_strategies_override_runs_only_the_given_strategies(monkeypatch):
    store = _FakeStore(title_hits={None: [_doc_hit("title-only", 1.0)]})
    calls: list = []
    _patch_vector(monkeypatch, [_chunk_hit("d1", "c1", 0.9)], calls)

    result = await retrieve("report_v2.pdf 리스크 항목 요약해줘", store=store, now=_NOW, strategies_override=["vector"])

    assert result.strategies == ["vector"]
    # **회귀 테스트**: `["vector"]`는 정말로 벡터 단독이어야 한다. 어휘 arm이 이름 없이
    # `vector()` 끝에 붙어 있던 동안 이 override는 document_search#semantic +
    # lexical_search#semantic까지 돌렸고(steps로 실측), "순수 벡터 baseline"이라고 적힌
    # docstring이 거짓이라 그 값으로 잰 A/B가 전부 무효였다. 그 드리프트를 막지 못한 것이
    # 바로 이 단언의 부재였다 - source까지 봐야 한다(step.strategy만 보면 arm의 step이
    # document_lookup/lexical로 찍혀 있어 눈치채기 어렵다).
    assert {s.source for s in result.steps} == {"vector_search"}
    assert store.title_calls == [] and store.chunk_calls == []
    assert result.documents_read == []
    assert result.document_ids == ["d1"]
    assert calls[0][0] == result.constraints["semantic_query"]


async def test_strategies_override_can_put_the_keyword_arm_back(monkeypatch):
    """위 테스트의 짝 - `["vector", "keyword_arm"]`이 기본 라우팅과 같은 하이브리드다
    (`suggest_strategies`는 vector를 넣을 때 arm을 항상 같이 넣는다). arm의 step은 실제로
    부른 검색을 따라 document_lookup/lexical로 찍히고 `arm="semantic_query"`로 구분된다."""
    store = _FakeStore(title_hits={None: [_doc_hit("title-only", 1.0)]})
    calls: list = []
    _patch_vector(monkeypatch, [_chunk_hit("d1", "c1", 0.9)], calls)

    result = await retrieve(
        "report_v2.pdf 리스크 항목 요약해줘", store=store, now=_NOW, strategies_override=["vector", "keyword_arm"]
    )

    # 실행 순서는 vector의 ANN 조회 → arm의 제목/본문 조회다 - `_claim_anchor`가 먼저 잡힌
    # anchor를 덮어쓰지 않아서, 이 순서가 바뀌면 지표가 움직인다.
    assert [s.strategy for s in result.steps] == ["vector", "document_lookup", "lexical"]
    assert [q for q, _limit in store.title_calls] == [result.constraints["semantic_query"]]
    # 'title-only'는 어휘 arm의 제목 조회가 물어온 것 - vector 히트 'd1'과 함께 RRF에 들어간다.
    # 점수 1.0(제목 substring 완전 일치)에 경쟁자가 없어 arm이 anchor로 삼으므로 최상위 고정
    # (retrieve.py::_claim_anchor - 제목이 정확히 맞은 문서는 벡터 유사도와 무관하게 답이다).
    assert result.document_ids == ["title-only", "d1"]


async def test_result_to_dict_is_json_serializable_and_carries_the_contract_fields(monkeypatch):
    _patch_vector(monkeypatch, [_chunk_hit("d1", "c1", 0.9)])

    result = await retrieve("지난주 배포 일정", store=_FakeStore(filter_rows=[_filter_row("d1", _NOW)]), now=_NOW)
    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))

    assert set(payload) == {
        "query", "constraints", "strategies", "steps", "hits", "document_ids", "scores",
        "predicted_count", "documents_read", "elapsed_ms",
    }
    assert payload["document_ids"] == ["d1"]
    assert len(payload["scores"]) == len(payload["document_ids"])
    assert all({"document_id", "target_kind", "score"} <= set(h) for h in payload["hits"])
    assert payload["elapsed_ms"] >= 0


# --- against Postgres --------------------------------------------------------------

_PROJECT = "test-retrieve"


@pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")
class TestAgainstPostgres:
    @pytest_asyncio.fixture
    async def store(self, tmp_path):
        from knowledge_forge.storage.postgres_store import PostgresStore
        from knowledge_forge.storage.store import PipelineStore

        aug = datetime(2026, 8, 20, tzinfo=timezone.utc)
        jun = datetime(2026, 6, 1, tzinfo=timezone.utc)
        mail_src = Source(
            source_id="test-rt-s1",
            source_type=SourceType.GMAIL,
            uri="gmail://thread/test-rt-1",
            content_hash="h-rt-1",
            collected_at=aug,
            modified_at=aug,
            metadata={"subject": "주간 업무 보고", "participants": ['"김철수" <kim@example.com>']},
            project_id=_PROJECT,
        )
        drive_src = Source(
            source_id="test-rt-s2",
            source_type=SourceType.GOOGLE_DRIVE,
            uri="gdrive://file/test-rt-2",
            content_hash="h-rt-2",
            collected_at=jun,
            modified_at=jun,
            metadata={"name": "2025_SH_업무_보고서.pdf"},
            project_id=_PROJECT,
        )
        docs = [
            Document(
                document_id="test-rt-d1",
                source=SourceRef.from_source(mail_src),
                elements=[TextBlock(element_id="e1", text="x")],
                metadata=DocumentMetadata(title="주간 업무 보고"),
            ),
            Document(
                document_id="test-rt-d2",
                source=SourceRef.from_source(drive_src),
                elements=[TextBlock(element_id="e1", text="x")],
                metadata=DocumentMetadata(title="2025_SH_업무_보고서.pdf"),
            ),
        ]
        chunks = [
            Chunk(
                chunk_id="test-rt-c1",
                document_id="test-rt-d1",
                source_id="test-rt-s1",
                hierarchy_level=HierarchyLevel.PARAGRAPH,
                sequence_index=0,
                text="배포 중 ERR_XYZ_123 오류가 발생해 롤백했습니다.",
                token_count=20,
                element_ids=["e1"],
            ),
            Chunk(
                chunk_id="test-rt-c2",
                document_id="test-rt-d2",
                source_id="test-rt-s2",
                hierarchy_level=HierarchyLevel.PARAGRAPH,
                sequence_index=0,
                text="상반기 실적 요약 및 하반기 계획",
                token_count=12,
                element_ids=["e1"],
            ),
            Chunk(
                chunk_id="test-rt-c3",
                document_id="test-rt-d2",
                source_id="test-rt-s2",
                hierarchy_level=HierarchyLevel.PARAGRAPH,
                sequence_index=1,
                text="리스크: 일정 지연",
                token_count=8,
                element_ids=["e1"],
            ),
        ]
        async with PostgresStore(DSN) as db:
            await db.init_schema()
            await db.save_source(mail_src, None)
            await db.save_source(drive_src, None)
            for doc in docs:
                await db.save_document(doc)
            await db.save_chunks(chunks)
        try:
            async with PipelineStore(DSN, tmp_path) as pipeline_store:
                yield pipeline_store
        finally:
            async with PostgresStore(DSN) as db:
                await db.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'test-rt-%'")
                await db.conn.execute("DELETE FROM document_links WHERE from_document_id LIKE 'test-rt-%' OR to_document_id LIKE 'test-rt-%'")
                await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-rt-%'")
                await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-rt-%'")

    async def test_title_reference_with_summary_anchors_and_reads_the_document(self, store, monkeypatch):
        _patch_vector(monkeypatch, [])

        result = await retrieve("2025_SH_업무_보고서 요약해줘", store=store, project_id=_PROJECT, now=_NOW)

        assert result.document_ids[0] == "test-rt-d2"
        assert result.documents_read == ["test-rt-d2"]
        assert [h["chunk_id"] for h in result.hits[:2]] == ["test-rt-c2", "test-rt-c3"]

    async def test_identifier_is_found_lexically_in_chunk_text(self, store, monkeypatch):
        _patch_vector(monkeypatch, [])

        result = await retrieve("ERR_XYZ_123 문서", store=store, project_id=_PROJECT, now=_NOW)

        lexical = next(s for s in result.steps if s.strategy == "lexical" and "fallback_from" not in s.detail)
        assert lexical.n_hits >= 1
        assert result.document_ids[0] == "test-rt-d1"
        assert result.hits[0]["chunk_id"] == "test-rt-c1"

    async def test_time_and_source_filter_selects_only_the_matching_document(self, store, monkeypatch):
        _patch_vector(monkeypatch, [])

        result = await retrieve("2026년 8월에 온 메일", store=store, project_id=_PROJECT, now=_NOW)

        step = next(s for s in result.steps if s.strategy == "metadata_filter")
        assert "s.source_type IN ('gmail')" in step.detail["sql"]
        assert result.document_ids == ["test-rt-d1"]

    async def test_author_filter_matches_gmail_participants(self, store, monkeypatch):
        _patch_vector(monkeypatch, [])

        result = await retrieve("김철수가 보낸 자료", store=store, project_id=_PROJECT, now=_NOW)

        assert result.document_ids == ["test-rt-d1"]
