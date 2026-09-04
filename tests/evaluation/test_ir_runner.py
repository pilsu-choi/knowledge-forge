"""`run_ir_eval` with a fake retriever and no DB - the runner's own logic
(per-query scoring, error isolation, filters, limit promotion, summary
shape) is what's under test, not `search/retrieve.py` or Postgres.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from knowledge_forge.evaluation.golden import GoldenExpected, GoldenQuery, GoldenSet, IndexSnapshot
from knowledge_forge.evaluation.ir_runner import (
    EXHAUSTIVE_LIMIT,
    config_hash,
    effective_limit,
    run_ir_eval,
    run_to_dict,
    select_queries,
)

AS_OF = datetime(2026, 9, 1, tzinfo=timezone.utc)


@dataclass
class FakeStep:
    strategy: str
    source: str
    n_hits: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeResult:
    document_ids: list[str]
    scores: list[float]
    predicted_count: int | None = None
    strategies: list[str] = field(default_factory=lambda: ["vector"])
    steps: list[FakeStep] = field(default_factory=lambda: [FakeStep("vector", "chunk", 3)])
    elapsed_ms: float = 12.5


def _golden(queries: list[GoldenQuery], project_id: str | None = None) -> GoldenSet:
    return GoldenSet(
        golden_version="vtest",
        created_at=AS_OF,
        project_id=project_id,
        index_snapshot=IndexSnapshot(documents=1, chunks=1, knowledge_objects=1),
        queries=queries,
    )


Q_FACTOID = GoldenQuery(
    id="q-factoid",
    query="what is A?",
    primary_type="factoid",
    types=["factoid", "entity_centric"],
    expected=GoldenExpected(document_ids=["A"]),
    source_hash="h-a",
)
Q_SET = GoldenQuery(
    id="q-set",
    query="list everything",
    primary_type="set_query",
    types=["set_query"],
    expected=GoldenExpected(document_ids=["A", "B"], exhaustive=True),
)
Q_COUNT = GoldenQuery(
    id="q-count",
    query="how many?",
    primary_type="aggregation",
    types=["aggregation"],
    expected=GoldenExpected(count=2),
)
Q_NULL = GoldenQuery(
    id="q-null",
    query="nothing here",
    primary_type="null",
    types=["null"],
    expected=GoldenExpected(none=True),
)
Q_LATEST = GoldenQuery(
    id="q-latest",
    query="most recent",
    primary_type="latest",
    types=["latest"],
    expected=GoldenExpected(document_ids=["B"]),
    as_of=AS_OF,
)


class RecordingRetriever:
    """Returns canned results keyed by query text and records every call's
    kwargs so the test can check what the runner passed through."""

    def __init__(self, results: dict[str, FakeResult | Exception]):
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, query: str, **kwargs: Any) -> FakeResult:
        self.calls.append({"query": query, **kwargs})
        outcome = self.results[query]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


STORE = object()


async def test_scores_each_query_by_its_expected_shape():
    retriever = RecordingRetriever(
        {
            "what is A?": FakeResult(["x", "A"], [0.9, 0.8]),
            "list everything": FakeResult(["A", "B", "C"], [0.9, 0.8, 0.1]),
            "how many?": FakeResult([], [], predicted_count=2),
            "nothing here": FakeResult([], []),
        }
    )
    run = await run_ir_eval(
        _golden([Q_FACTOID, Q_SET, Q_COUNT, Q_NULL]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,
        golden_path="g.json",
    )

    by_id = {q.query_id: q for q in run.queries}
    assert by_id["q-factoid"].metrics["mrr"] == 0.5
    assert by_id["q-factoid"].metrics["hit@1"] == 0.0
    assert by_id["q-set"].metrics["exhaustive_recall"] == 1.0
    assert by_id["q-set"].metrics["exhaustive_precision"] == pytest.approx(2 / 3)
    assert by_id["q-count"].metrics == {"count_match": 1.0}
    assert by_id["q-null"].metrics == {"none_correct": 1.0}
    assert by_id["q-factoid"].ranked_document_ids == ["x", "A"]
    assert by_id["q-factoid"].strategies == ["vector"]
    assert by_id["q-factoid"].steps == [{"strategy": "vector", "source": "chunk", "n_hits": 3, "detail": {}}]
    assert by_id["q-factoid"].elapsed_ms == 12.5
    assert all(q.error is None for q in run.queries)

    assert run.golden_version == "vtest"
    assert run.golden_path == "g.json"
    assert run.summary["overall"]["n"] == 4
    assert run.summary["overall"]["mrr"] == 0.75  # (0.5 + 1.0) / 2 over the two ranked queries
    assert run.summary["by_type"]["entity_centric"]["n"] == 1  # multi-label: factoid also counts here
    assert run.summary["errors"] == []
    assert run.summary["staleness_checked"] is False
    assert run.finished_at is not None and run.finished_at >= run.started_at


async def test_retrieval_error_is_isolated_to_that_query():
    retriever = RecordingRetriever(
        {
            "what is A?": RuntimeError("vector index down"),
            "nothing here": FakeResult([], []),
        }
    )
    run = await run_ir_eval(
        _golden([Q_FACTOID, Q_NULL]), store=STORE, eval_store=None, retrieve_fn=retriever  # type: ignore[arg-type]
    )

    failed = run.queries[0]
    assert failed.query_id == "q-factoid"
    assert failed.error is not None and "vector index down" in failed.error
    assert failed.metrics == {}
    assert run.queries[1].metrics == {"none_correct": 1.0}
    # The failed query drops out of the aggregate rather than scoring zero.
    assert run.summary["overall"]["n"] == 1
    assert run.summary["errors"] == [{"query_id": "q-factoid", "error": failed.error}]


async def test_limit_promoted_for_exhaustive_and_count_and_as_of_passed_as_now():
    retriever = RecordingRetriever(
        {
            "what is A?": FakeResult(["A"], [1.0]),
            "list everything": FakeResult(["A", "B"], [1.0, 0.9]),
            "how many?": FakeResult([], [], predicted_count=1),
            "most recent": FakeResult(["B"], [1.0]),
        }
    )
    await run_ir_eval(
        _golden([Q_FACTOID, Q_SET, Q_COUNT, Q_LATEST], project_id="proj-g"),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,
        project_id="proj-cli",
        limit=7,
        strategies_override=["lexical"],
        use_llm_constraints=False,
        concurrency=1,
    )

    calls = {c["query"]: c for c in retriever.calls}
    assert calls["what is A?"]["limit"] == 7
    assert calls["list everything"]["limit"] == EXHAUSTIVE_LIMIT
    assert calls["how many?"]["limit"] == EXHAUSTIVE_LIMIT
    assert calls["most recent"]["now"] == AS_OF
    assert calls["what is A?"]["now"] is None
    # The gold file's project_id wins over the CLI's.
    assert {c["project_id"] for c in retriever.calls} == {"proj-g"}
    assert {c["strategies_override"][0] for c in retriever.calls} == {"lexical"}
    assert {c["use_llm_constraints"] for c in retriever.calls} == {False}
    assert {c["store"] is STORE for c in retriever.calls} == {True}


def test_effective_limit_never_lowers_a_larger_limit():
    assert effective_limit(Q_SET, 500) == 500
    assert effective_limit(Q_FACTOID, 500) == 500


def test_select_queries_by_id_and_by_any_type_label():
    golden = _golden([Q_FACTOID, Q_SET, Q_NULL])
    assert [q.id for q in select_queries(golden, query_filter={"q-null"})] == ["q-null"]
    # entity_centric is only a secondary label on q-factoid - still matches.
    assert [q.id for q in select_queries(golden, type_filter={"entity_centric"})] == ["q-factoid"]
    assert [q.id for q in select_queries(golden, query_filter={"q-set", "q-null"}, type_filter={"null"})] == ["q-null"]
    assert select_queries(golden, type_filter={"multihop"}) == []


def test_select_queries_by_split_and_refuses_an_unsplit_set():
    """분할 없는 세트에 `--split test`를 걸면 조용히 0개를 채점하는 대신
    에러여야 한다 - 헤드라인 숫자가 엉뚱한 모집단 위에서 나오는 걸 막는다."""
    unsplit = _golden([Q_FACTOID, Q_SET, Q_NULL])
    with pytest.raises(ValueError, match="no dev/test split"):
        select_queries(unsplit, split_filter="test")

    split = _golden(
        [
            Q_FACTOID.model_copy(update={"split": "dev"}),
            Q_SET.model_copy(update={"split": "test"}),
            Q_NULL.model_copy(update={"split": "test"}),
        ]
    )
    assert [q.id for q in select_queries(split, split_filter="dev")] == ["q-factoid"]
    assert [q.id for q in select_queries(split, split_filter="test")] == ["q-set", "q-null"]
    # 다른 필터와 함께 걸면 교집합이다.
    assert [q.id for q in select_queries(split, type_filter={"null"}, split_filter="test")] == ["q-null"]
    with pytest.raises(ValueError, match="split_filter must be"):
        select_queries(split, split_filter="holdout")


async def test_filters_restrict_what_runs_but_types_by_id_still_covers_them():
    retriever = RecordingRetriever({"nothing here": FakeResult([], [])})
    run = await run_ir_eval(
        _golden([Q_FACTOID, Q_NULL]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,
        query_filter={"q-null"},
    )
    assert [c["query"] for c in retriever.calls] == ["nothing here"]
    assert run.summary["n_selected"] == 1
    assert run.summary["by_type"] == {"null": {"n": 1, "none_correct": 1.0}}


async def test_staleness_reported_but_not_excluded():
    retriever = RecordingRetriever({"what is A?": FakeResult(["A"], [1.0]), "list everything": FakeResult(["A", "B"], [1, 1])})
    run = await run_ir_eval(
        _golden([Q_FACTOID, Q_SET]),
        store=STORE,  # type: ignore[arg-type]
        eval_store=None,
        retrieve_fn=retriever,
        content_hashes={"A": "h-different"},  # A changed, B missing; q-set carries no source_hash
    )
    assert run.summary["staleness_checked"] is True
    assert run.summary["stale_queries"] == [
        {"query_id": "q-factoid", "reason": "changed", "document_ids": ["A"]},
        {"query_id": "q-set", "reason": "missing", "document_ids": ["B"]},
    ]
    assert run.summary["overall"]["n"] == 2  # still scored


async def test_config_hash_is_stable_and_records_names():
    retriever = RecordingRetriever({"nothing here": FakeResult([], [])})
    golden = _golden([Q_NULL])
    kwargs: dict[str, Any] = {"store": STORE, "eval_store": None, "retrieve_fn": retriever}
    run1 = await run_ir_eval(golden, provider_name="anthropic", model_name="m", limit=5, k_values=(1, 5), **kwargs)
    run2 = await run_ir_eval(golden, provider_name="anthropic", model_name="m", limit=5, k_values=(1, 5), **kwargs)
    run3 = await run_ir_eval(golden, provider_name="anthropic", model_name="m", limit=6, k_values=(1, 5), **kwargs)

    assert run1.config == {
        "provider": "anthropic",
        "model": "m",
        "limit": 5,
        "strategies_override": None,
        "use_llm_constraints": True,
        "k_values": [1, 5],
        "split": None,
    }
    assert run1.config_hash == run2.config_hash == config_hash(run1.config)
    assert run1.config_hash != run3.config_hash
    assert len(run1.config_hash) == 12
    assert run1.run_id != run2.run_id


async def test_run_to_dict_includes_queries_and_is_json_safe():
    import json

    retriever = RecordingRetriever({"most recent": FakeResult(["B"], [1.0])})
    run = await run_ir_eval(_golden([Q_LATEST]), store=STORE, eval_store=None, retrieve_fn=retriever)  # type: ignore[arg-type]
    payload = json.loads(json.dumps(run_to_dict(run)))
    assert payload["run_id"] == run.run_id
    assert payload["queries"][0]["query_id"] == "q-latest"
    assert payload["queries"][0]["metrics"]["hit@1"] == 1.0
    assert "queries" not in run.model_dump()  # excluded from the DB-facing dump


async def test_run_records_the_code_that_actually_ran(monkeypatch):
    """`git_commit`은 HEAD가 아니라 "돌아간 코드"를 적는다. 커밋 b470057 하나 밑에
    ndcg@10 0.736(ablate-vector)과 0.477(purity-vector-only)이 같은 값으로 쌓였던
    사고가 이 컬럼의 의미를 바꾼 이유다. (git 자체를 태우는 검증은
    tests/evaluation/test_code_provenance.py.)"""
    import knowledge_forge.evaluation.ir_runner as ir_runner

    prov = ir_runner.CodeProvenance(commit="b" * 40, dirty_files=[" M src/x.py"], diff_hash="0123456789ab")
    monkeypatch.setattr(ir_runner, "current_code_provenance", lambda: prov)
    retriever = RecordingRetriever({"nothing here": FakeResult([], [])})
    run = await run_ir_eval(_golden([Q_NULL]), store=STORE, eval_store=None, retrieve_fn=retriever)  # type: ignore[arg-type]

    assert run.git_commit == "b" * 40 + "+dirty.0123456789ab"
    assert run.dirty_files == [" M src/x.py"]


async def test_clean_run_leaves_dirty_files_null(monkeypatch):
    import knowledge_forge.evaluation.ir_runner as ir_runner

    prov = ir_runner.CodeProvenance(commit="c" * 40, dirty_files=[], diff_hash=None)
    monkeypatch.setattr(ir_runner, "current_code_provenance", lambda: prov)
    retriever = RecordingRetriever({"nothing here": FakeResult([], [])})
    run = await run_ir_eval(_golden([Q_NULL]), store=STORE, eval_store=None, retrieve_fn=retriever)  # type: ignore[arg-type]

    # 깨끗한 run은 예전과 똑같이 맨 sha만 남고, dirty_files는 NULL로 들어간다.
    assert run.git_commit == "c" * 40
    assert run.dirty_files is None


async def test_missing_retriever_module_surfaces_as_import_error(monkeypatch):
    # With no retrieve_fn the runner resolves search.retrieve lazily; a
    # missing module must fail loudly at call time, not at import time.
    import knowledge_forge.evaluation.ir_runner as ir_runner

    def _boom():
        raise ImportError("no retrieve module")

    monkeypatch.setattr(ir_runner, "default_retrieve_fn", _boom)
    with pytest.raises(ImportError):
        await run_ir_eval(_golden([Q_NULL]), store=STORE, eval_store=None)  # type: ignore[arg-type]
