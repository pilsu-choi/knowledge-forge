import math

import pytest

from knowledge_forge.evaluation.golden import GoldenExpected
from knowledge_forge.evaluation.ir_metrics import (
    aggregate,
    bootstrap_ci,
    count_match,
    dedupe_ranked,
    exhaustive_precision,
    exhaustive_recall,
    hit_at_k,
    mrr,
    ndcg_at_k,
    none_correct,
    paired_delta,
    precision_at_k,
    recall_at_k,
    score_query,
)

# relevant = {a, b, c}; ranked = [x, a, a, y, b, c]  ->  deduped [x, a, y, b, c]
REL = GoldenExpected(document_ids=["a", "b", "c"])
RANKED = ["x", "a", "a", "y", "b", "c"]


def test_dedupe_keeps_first_occurrence_order():
    assert dedupe_ranked(RANKED) == ["x", "a", "y", "b", "c"]


def test_recall_at_k_hand_computed():
    assert recall_at_k(RANKED, REL, 1) == 0.0
    assert recall_at_k(RANKED, REL, 2) == pytest.approx(1 / 3)
    # duplicates must not push 'b' past the cut-off: deduped top-3 = x,a,y
    assert recall_at_k(RANKED, REL, 3) == pytest.approx(1 / 3)
    assert recall_at_k(RANKED, REL, 4) == pytest.approx(2 / 3)
    assert recall_at_k(RANKED, REL, 10) == 1.0


def test_precision_at_k_denominator_is_k():
    assert precision_at_k(RANKED, REL, 2) == 0.5
    assert precision_at_k(RANKED, REL, 5) == pytest.approx(3 / 5)
    assert precision_at_k(RANKED, REL, 10) == pytest.approx(3 / 10)
    assert precision_at_k([], REL, 5) == 0.0


def test_hit_and_mrr():
    assert hit_at_k(RANKED, REL, 1) == 0.0
    assert hit_at_k(RANKED, REL, 2) == 1.0
    assert mrr(RANKED, REL) == 0.5
    assert mrr(["c"], REL) == 1.0
    assert mrr(["x", "y"], REL) == 0.0


def test_ndcg_hand_computed_example():
    # deduped top-5 = [x, a, y, b, c]: gains 0,1,0,1,1
    dcg = 1 / math.log2(3) + 1 / math.log2(5) + 1 / math.log2(6)
    idcg = 1 / math.log2(2) + 1 / math.log2(3) + 1 / math.log2(4)
    assert ndcg_at_k(RANKED, REL, 5) == pytest.approx(dcg / idcg)
    assert ndcg_at_k(["a", "b", "c"], REL, 3) == 1.0
    assert ndcg_at_k(["x"], REL, 3) == 0.0
    # k smaller than |relevant|: ideal only counts k hits
    assert ndcg_at_k(["a"], REL, 1) == 1.0


def test_exhaustive_recall_and_precision_use_whole_list():
    exp = GoldenExpected(document_ids=["a", "b", "c"], exhaustive=True)
    assert exhaustive_recall(["a"] + ["z"] * 20 + ["b", "c"], exp) == 1.0
    assert exhaustive_recall(["a", "b"], exp) == pytest.approx(2 / 3)
    assert exhaustive_precision(["a", "z", "b", "b"], exp) == pytest.approx(2 / 3)
    assert exhaustive_precision([], exp) == 0.0


def test_count_match_and_none_correct():
    agg = GoldenExpected(count=6)
    assert count_match(6, agg) == 1.0
    assert count_match(5, agg) == 0.0
    assert count_match(None, agg) == 0.0
    with pytest.raises(ValueError):
        count_match(1, REL)

    none = GoldenExpected(none=True)
    assert none_correct([], none) == 1.0
    assert none_correct(["x"], none) == 0.0
    assert none_correct(["x", "y"], none, scores=[0.2, 0.1], threshold=0.3) == 1.0
    assert none_correct(["x", "y"], none, scores=[0.5, 0.1], threshold=0.3) == 0.0
    with pytest.raises(ValueError):
        none_correct([], REL)


def test_relevance_metrics_refuse_none_expectation():
    none = GoldenExpected(none=True)
    for fn in (recall_at_k, precision_at_k, hit_at_k, ndcg_at_k):
        with pytest.raises(ValueError):
            fn(["x"], none, 3)
    with pytest.raises(ValueError):
        mrr(["x"], none)
    with pytest.raises(ValueError):
        recall_at_k(["x"], REL, 0)


def test_score_query_dispatches_by_shape():
    plain = score_query(RANKED, REL, k_values=(1, 5))
    assert set(plain) == {"recall@1", "precision@1", "hit@1", "ndcg@1", "recall@5", "precision@5", "hit@5", "ndcg@5", "mrr"}

    exhaustive = score_query(["a", "b"], GoldenExpected(document_ids=["a", "b", "c"], exhaustive=True), k_values=(3,))
    assert exhaustive["exhaustive_recall"] == pytest.approx(2 / 3)
    assert exhaustive["exhaustive_complete"] == 0.0
    assert exhaustive["exhaustive_precision"] == 1.0

    agg = score_query(["a"], GoldenExpected(document_ids=["a"], count=1), k_values=(1,), predicted_count=1)
    assert agg["count_match"] == 1.0
    assert "count_match" not in score_query(["a"], GoldenExpected(document_ids=["a"], count=1), k_values=(1,))
    assert set(score_query([], GoldenExpected(count=4), k_values=(1,), predicted_count=3)) == {"count_match"}

    assert score_query(["x"], GoldenExpected(none=True)) == {"none_correct": 0.0}

    chunked = score_query(
        ["a"], GoldenExpected(document_ids=["a"], chunk_ids=["c9"]), k_values=(1, 3), ranked_chunk_ids=["c1", "c9"]
    )
    assert chunked["chunk_hit@1"] == 0.0 and chunked["chunk_hit@3"] == 1.0 and chunked["chunk_mrr"] == 0.5


def test_aggregate_overall_and_per_type_multilabel():
    per_query = [
        {"id": "q1", "metrics": {"mrr": 1.0, "recall@5": 1.0}},
        {"id": "q2", "metrics": {"mrr": 0.5, "recall@5": 0.0}},
        {"id": "q3", "metrics": {"none_correct": 1.0}},
    ]
    types = {"q1": ["factoid", "temporal"], "q2": ["temporal"], "q3": ["null"]}

    out = aggregate(per_query, types)

    assert out["overall"]["n"] == 3
    assert out["overall"]["mrr"] == pytest.approx(0.75)
    assert out["overall"]["none_correct"] == 1.0
    assert out["metric_n"] == {"mrr": 2, "none_correct": 1, "recall@5": 2}
    assert out["by_type"]["factoid"] == {"n": 1, "mrr": 1.0, "recall@5": 1.0}
    assert out["by_type"]["temporal"]["n"] == 2
    assert out["by_type"]["temporal"]["mrr"] == pytest.approx(0.75)
    assert out["by_type"]["null"] == {"n": 1, "none_correct": 1.0}


def test_paired_delta_only_over_shared_queries_and_metrics():
    run_a = {"q1": {"mrr": 0.5, "recall@5": 1.0}, "q2": {"mrr": 1.0}, "only_a": {"mrr": 0.0}}
    run_b = {"q1": {"mrr": 1.0, "recall@5": 1.0}, "q2": {"mrr": 0.5, "recall@5": 1.0}, "only_b": {"mrr": 1.0}}

    out = paired_delta(run_a, run_b)

    assert out["mrr"]["query_ids"] == ["q1", "q2"]
    assert out["mrr"]["deltas"] == [0.5, -0.5]
    assert out["mrr"]["mean_delta"] == 0.0
    assert (out["mrr"]["wins"], out["mrr"]["losses"], out["mrr"]["ties"]) == (1, 1, 0)
    assert out["recall@5"] == {
        "query_ids": ["q1"], "deltas": [0.0], "n": 1, "mean_delta": 0.0, "wins": 0, "losses": 0, "ties": 1
    }


def test_bootstrap_ci_is_deterministic_and_brackets_mean():
    deltas = [0.1, -0.2, 0.3, 0.0, 0.25, -0.05, 0.15, 0.1]
    lo, hi = bootstrap_ci(deltas, n_boot=500, seed=42)
    assert (lo, hi) == bootstrap_ci(deltas, n_boot=500, seed=42)
    assert bootstrap_ci(deltas, n_boot=500, seed=7) != (lo, hi)
    assert lo <= sum(deltas) / len(deltas) <= hi
    assert bootstrap_ci([0.2, 0.2, 0.2], n_boot=50) == pytest.approx((0.2, 0.2))
    with pytest.raises(ValueError):
        bootstrap_ci([], n_boot=10)
