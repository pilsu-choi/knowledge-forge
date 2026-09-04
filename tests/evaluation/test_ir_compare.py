"""`compare_runs` against hand-computed deltas - no DB, the EvalRun/
EvalRunQuery rows are built inline the way `list_eval_run_queries` would
return them.
"""

from datetime import datetime, timezone

import pytest

from knowledge_forge.evaluation.ir_compare import compare_runs
from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
SNAP = {"documents": 10, "chunks": 20, "knowledge_objects": 30, "embeddings_by_kind": {"chunk": 20}}


def _run(run_id: str, *, golden_version: str = "v0", snapshot: dict = SNAP, config_hash: str = "cfg") -> EvalRun:
    return EvalRun(
        run_id=run_id,
        golden_version=golden_version,
        golden_path="v0.json",
        config_hash=config_hash,
        index_snapshot=snapshot,
        started_at=NOW,
        finished_at=NOW,
    )


def _q(run_id: str, qid: str, metrics: dict[str, float], types: list[str] | None = None, error: str | None = None):
    return EvalRunQuery(
        run_id=run_id,
        query_id=qid,
        query_text=qid,
        primary_type=(types or ["factoid"])[0],
        types=types or ["factoid"],
        metrics=metrics,
        error=error,
    )


A = _run("a", config_hash="cfg-a")
B = _run("b", config_hash="cfg-b")
QA = [
    _q("a", "q1", {"ndcg@10": 0.5, "mrr": 0.5}, ["factoid", "entity_centric"]),
    _q("a", "q2", {"ndcg@10": 1.0, "mrr": 1.0}, ["navigational"]),
    _q("a", "q3", {"ndcg@10": 0.0, "mrr": 0.0}, ["factoid"]),
    _q("a", "q4", {"none_correct": 1.0}, ["null"]),
]
QB = [
    _q("b", "q1", {"ndcg@10": 1.0, "mrr": 1.0}, ["factoid", "entity_centric"]),  # +0.5
    _q("b", "q2", {"ndcg@10": 0.5, "mrr": 0.5}, ["navigational"]),  # -0.5
    _q("b", "q3", {"ndcg@10": 0.0, "mrr": 0.0}, ["factoid"]),  # 0
    _q("b", "q4", {"none_correct": 0.0}, ["null"]),
]


def test_headline_metric_deltas_and_win_loss_tie_by_hand():
    report = compare_runs(A, QA, B, QB, metric="ndcg@10", n_boot=200, seed=1)

    assert report["golden_version"] == "v0"
    assert report["n"] == 3  # q4 has no ndcg@10
    assert report["mean_a"] == pytest.approx(0.5)
    assert report["mean_b"] == pytest.approx(0.5)
    assert report["mean_delta"] == pytest.approx(0.0)
    assert (report["wins"], report["losses"], report["ties"]) == (1, 1, 1)
    assert [q["query_id"] for q in report["per_query"]] == ["q1", "q2", "q3"]
    assert [q["delta"] for q in report["per_query"]] == [0.5, -0.5, 0.0]
    assert report["by_type"]["factoid"] == {"n": 2, "mean_delta": 0.25, "wins": 1, "losses": 0, "ties": 1}
    assert report["by_type"]["entity_centric"] == {"n": 1, "mean_delta": 0.5, "wins": 1, "losses": 0, "ties": 0}
    assert report["by_type"]["navigational"]["mean_delta"] == -0.5
    assert "null" not in report["by_type"]
    lo, hi = report["ci95"]
    assert lo <= 0.0 <= hi
    assert report["significant"] is False
    assert report["snapshot_mismatch"] is False
    assert report["warnings"] == []
    # every shared metric gets a mean-delta row, including none_correct (q4 only)
    assert report["per_metric"]["none_correct"] == {"n": 1, "mean_delta": -1.0, "wins": 0, "losses": 1, "ties": 0}
    assert report["per_metric"]["mrr"]["n"] == 3


def test_rejects_different_golden_versions():
    with pytest.raises(ValueError, match="golden versions"):
        compare_runs(A, QA, _run("b", golden_version="v1"), QB)


def test_snapshot_mismatch_is_a_warning_not_an_error():
    other = _run("b", snapshot={**SNAP, "documents": 11}, config_hash="cfg-b")
    report = compare_runs(A, QA, other, QB)
    assert report["snapshot_mismatch"] is True
    assert any("index_snapshot" in w for w in report["warnings"])


def test_same_config_hash_warns_about_noise():
    report = compare_runs(A, QA, _run("b", config_hash="cfg-a"), QB)
    assert any("config_hash" in w for w in report["warnings"])


def test_errored_queries_drop_out_of_the_pairing():
    qb = [*QB[:2], _q("b", "q3", {}, ["factoid"], error="boom"), QB[3]]
    report = compare_runs(A, QA, B, qb)
    assert report["n"] == 2
    assert [q["query_id"] for q in report["per_query"]] == ["q1", "q2"]


def test_no_shared_metric_gives_empty_report():
    report = compare_runs(A, QA, B, QB, metric="chunk_mrr")
    assert report["n"] == 0
    assert report["per_query"] == []
    assert report["mean_delta"] is None


def test_deterministic_for_seed():
    qb = [_q("b", "q1", {"ndcg@10": 0.9}), _q("b", "q2", {"ndcg@10": 0.7}), _q("b", "q3", {"ndcg@10": 0.4})]
    r1 = compare_runs(A, QA, B, qb, n_boot=300, seed=7)
    r2 = compare_runs(A, QA, B, qb, n_boot=300, seed=7)
    r3 = compare_runs(A, QA, B, qb, n_boot=300, seed=8)
    assert r1["ci95"] == r2["ci95"]
    assert r1["mean_delta"] == pytest.approx((0.4 - 0.3 + 0.4) / 3)
    assert r1["mean_delta"] == r3["mean_delta"]  # only the CI depends on the seed
