"""Pure IR metrics over a ranked list of document_ids and a `GoldenExpected`
(wiki/search-evaluation.md (C)). No DB, no LLM, no numpy - so the arithmetic
can be unit-tested against hand-computed values and the paired A/B stats
are reproducible from a seed alone.

Conventions shared by every function:

- `ranked_ids` is the retrieval output in rank order and **may contain
  duplicates** (chunk-grain search returns several hits per document);
  only the first occurrence of an id counts, later ones are dropped before
  any cut-off is applied. This is what makes KO-grain and chunk-grain
  results comparable on the same gold set.
- Relevance is binary (`expected.document_ids` membership) - the gold set
  carries no graded labels, so NDCG uses gains of 1/0.
- Functions that need a relevant set raise `ValueError` on a none-type
  expectation instead of returning 0.0: a silent zero would drag the
  per-type averages down for the wrong reason. `score_query` does the
  dispatch so callers don't have to.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from typing import TypedDict

from knowledge_forge.evaluation.golden import GoldenExpected

DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)


def dedupe_ranked(ranked_ids: Iterable[str]) -> list[str]:
    """First occurrence wins; order otherwise preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for rid in ranked_ids:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def _relevant(expected: GoldenExpected) -> set[str]:
    if expected.none or not expected.document_ids:
        raise ValueError("expected has no relevant document_ids (use none_correct / count_match instead)")
    return set(expected.document_ids)


def _check_k(k: int) -> None:
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")


def recall_at_k(ranked_ids: Sequence[str], expected: GoldenExpected, k: int) -> float:
    _check_k(k)
    relevant = _relevant(expected)
    top = dedupe_ranked(ranked_ids)[:k]
    return sum(1 for rid in top if rid in relevant) / len(relevant)


def precision_at_k(ranked_ids: Sequence[str], expected: GoldenExpected, k: int) -> float:
    """Denominator is k even when fewer than k results came back - returning
    fewer results is not rewarded."""
    _check_k(k)
    relevant = _relevant(expected)
    top = dedupe_ranked(ranked_ids)[:k]
    return sum(1 for rid in top if rid in relevant) / k


def hit_at_k(ranked_ids: Sequence[str], expected: GoldenExpected, k: int) -> float:
    _check_k(k)
    relevant = _relevant(expected)
    return 1.0 if any(rid in relevant for rid in dedupe_ranked(ranked_ids)[:k]) else 0.0


def mrr(ranked_ids: Sequence[str], expected: GoldenExpected) -> float:
    """Reciprocal rank of the first relevant hit over the *whole* list (no
    cut-off); 0.0 when nothing relevant was retrieved."""
    relevant = _relevant(expected)
    for rank, rid in enumerate(dedupe_ranked(ranked_ids), start=1):
        if rid in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[str], expected: GoldenExpected, k: int) -> float:
    """Binary-relevance NDCG: DCG = sum(rel_i / log2(i + 1)), IDCG assumes the
    min(|relevant|, k) relevant docs occupy the top ranks."""
    _check_k(k)
    relevant = _relevant(expected)
    top = dedupe_ranked(ranked_ids)[:k]
    dcg = sum(1.0 / math.log2(rank + 1) for rank, rid in enumerate(top, start=1) if rid in relevant)
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def exhaustive_recall(ranked_ids: Sequence[str], expected: GoldenExpected) -> float:
    """Recall against the complete relevant set over the *entire* result list
    (no @k) - the metric for `exhaustive=true` queries where every answer
    document must come back, not just one of them."""
    relevant = _relevant(expected)
    retrieved = set(dedupe_ranked(ranked_ids))
    return len(relevant & retrieved) / len(relevant)


def exhaustive_precision(ranked_ids: Sequence[str], expected: GoldenExpected) -> float:
    """Companion to `exhaustive_recall`: share of everything returned that is
    relevant. 0.0 for an empty result list."""
    relevant = _relevant(expected)
    retrieved = dedupe_ranked(ranked_ids)
    if not retrieved:
        return 0.0
    return sum(1 for rid in retrieved if rid in relevant) / len(retrieved)


def count_match(predicted_count: int | None, expected: GoldenExpected) -> float:
    if expected.count is None:
        raise ValueError("expected carries no count")
    return 1.0 if predicted_count == expected.count else 0.0


def none_correct(
    ranked_ids: Sequence[str],
    expected: GoldenExpected,
    *,
    scores: Sequence[float] | None = None,
    threshold: float | None = None,
) -> float:
    """"No relevant document" is the right answer. Correct when the result
    list is empty, or - if the caller supplies per-result `scores` and a
    `threshold` - when every returned score is at or below the threshold
    (the retriever surfaced only low-confidence noise). The threshold is the
    caller's decision; there is no default because it depends on the score
    scale of the retriever under test."""
    if not expected.none:
        raise ValueError("expected is not a none-type expectation")
    if not ranked_ids:
        return 1.0
    if scores is not None and threshold is not None:
        if len(scores) != len(ranked_ids):
            raise ValueError("scores must align with ranked_ids")
        return 1.0 if max(scores) <= threshold else 0.0
    return 0.0


def score_query(
    ranked_ids: Sequence[str],
    expected: GoldenExpected,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
    *,
    predicted_count: int | None = None,
    scores: Sequence[float] | None = None,
    none_threshold: float | None = None,
    ranked_chunk_ids: Sequence[str] | None = None,
) -> dict[str, float]:
    """Bundle only the metrics that make sense for this expectation's shape.

    - none-type: {"none_correct"}
    - otherwise: recall/precision/hit/ndcg @k for each k, mrr
      - exhaustive: + exhaustive_recall / exhaustive_precision / exhaustive_complete
      - count present and predicted_count given: + count_match
      - chunk_ids present and ranked_chunk_ids given: + chunk_hit@k / chunk_mrr
        (strict chunk-grain scoring, only when the mode under test exposes
        chunk ids)
    """
    if expected.none:
        return {"none_correct": none_correct(ranked_ids, expected, scores=scores, threshold=none_threshold)}

    out: dict[str, float] = {}
    if expected.document_ids:
        for k in k_values:
            out[f"recall@{k}"] = recall_at_k(ranked_ids, expected, k)
            out[f"precision@{k}"] = precision_at_k(ranked_ids, expected, k)
            out[f"hit@{k}"] = hit_at_k(ranked_ids, expected, k)
            out[f"ndcg@{k}"] = ndcg_at_k(ranked_ids, expected, k)
        out["mrr"] = mrr(ranked_ids, expected)
        if expected.exhaustive:
            rec = exhaustive_recall(ranked_ids, expected)
            out["exhaustive_recall"] = rec
            out["exhaustive_precision"] = exhaustive_precision(ranked_ids, expected)
            out["exhaustive_complete"] = 1.0 if rec == 1.0 else 0.0
    if expected.count is not None and predicted_count is not None:
        out["count_match"] = count_match(predicted_count, expected)
    if expected.chunk_ids and ranked_chunk_ids is not None:
        chunk_expected = GoldenExpected(document_ids=list(expected.chunk_ids))
        for k in k_values:
            out[f"chunk_hit@{k}"] = hit_at_k(ranked_chunk_ids, chunk_expected, k)
        out["chunk_mrr"] = mrr(ranked_chunk_ids, chunk_expected)
    return out


class QueryScore(TypedDict):
    id: str
    metrics: dict[str, float]


def _mean_by_metric(scores: Sequence[QueryScore]) -> dict[str, float]:
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for s in scores:
        for name, value in s["metrics"].items():
            sums[name] = sums.get(name, 0.0) + value
            counts[name] = counts.get(name, 0) + 1
    return {name: sums[name] / counts[name] for name in sorted(sums)}


def aggregate(per_query: Sequence[QueryScore], types_by_id: Mapping[str, Sequence[str]]) -> dict:
    """Overall and per-type means. A query contributes to every type it is
    labelled with (multi-label), so per-type `n`s do not sum to the overall
    `n`. Each metric's mean is over the queries that *have* that metric
    (e.g. `none_correct` only over none-type queries), which is why the
    per-metric `n` is reported alongside the group `n`."""
    overall = {"n": len(per_query), **_mean_by_metric(per_query)}
    by_type: dict[str, dict[str, float]] = {}
    grouped: dict[str, list[QueryScore]] = {}
    for s in per_query:
        for t in types_by_id.get(s["id"], ()):
            grouped.setdefault(t, []).append(s)
    for t in sorted(grouped):
        by_type[t] = {"n": len(grouped[t]), **_mean_by_metric(grouped[t])}
    metric_n: dict[str, int] = {}
    for s in per_query:
        for name in s["metrics"]:
            metric_n[name] = metric_n.get(name, 0) + 1
    return {"overall": overall, "by_type": by_type, "metric_n": dict(sorted(metric_n.items()))}


def paired_delta(
    run_a: Mapping[str, Mapping[str, float]],
    run_b: Mapping[str, Mapping[str, float]],
) -> dict[str, dict]:
    """Per-query B - A on the queries and metrics both runs share. Returns,
    per metric: the ordered delta list (sorted by query id, so the same input
    always yields the same list for `bootstrap_ci`), the mean, and
    win/loss/tie counts from B's point of view."""
    common_ids = sorted(set(run_a) & set(run_b))
    out: dict[str, dict] = {}
    metric_names = sorted({m for qid in common_ids for m in run_a[qid] if m in run_b[qid]})
    for name in metric_names:
        ids = [qid for qid in common_ids if name in run_a[qid] and name in run_b[qid]]
        deltas = [run_b[qid][name] - run_a[qid][name] for qid in ids]
        out[name] = {
            "query_ids": ids,
            "deltas": deltas,
            "n": len(deltas),
            "mean_delta": sum(deltas) / len(deltas) if deltas else 0.0,
            "wins": sum(1 for d in deltas if d > 0),
            "losses": sum(1 for d in deltas if d < 0),
            "ties": sum(1 for d in deltas if d == 0),
        }
    return out


def bootstrap_ci(
    deltas: Sequence[float],
    n_boot: int = 1000,
    seed: int = 0,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean delta. Deterministic for a given
    (deltas, n_boot, seed) - `random.Random(seed)` rather than the global RNG
    so two eval reports on the same runs agree to the last digit."""
    if not deltas:
        raise ValueError("deltas is empty")
    if n_boot < 1:
        raise ValueError("n_boot must be >= 1")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    rng = random.Random(seed)
    n = len(deltas)
    means = sorted(sum(rng.choice(deltas) for _ in range(n)) / n for _ in range(n_boot))
    alpha = (1 - confidence) / 2
    lo_idx = max(0, min(n_boot - 1, int(math.floor(alpha * (n_boot - 1)))))
    hi_idx = max(0, min(n_boot - 1, int(math.ceil((1 - alpha) * (n_boot - 1)))))
    return means[lo_idx], means[hi_idx]
