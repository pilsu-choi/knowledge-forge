"""Paired A/B comparison of two `EvalRun`s (wiki/search-evaluation.md (C)).

Why *paired*: two runs over the same gold file score the same queries, so
the per-query difference B - A is the signal and the between-query
variance (some queries are just hard) cancels out. Reporting only the two
overall means would hide a 0.03 gain inside a 0.2 spread. The bootstrap
CI over those deltas (seeded, so the report is reproducible) is the "is
this real?" check; win/loss/tie is the human-readable version.

Comparability rules, enforced here rather than trusted to the caller:

- different `golden_version` -> `ValueError`. The queries are not the same
  population, so a paired delta is meaningless.
- different `index_snapshot` -> allowed but flagged (`snapshot_mismatch`,
  `warnings`). A re-ingested index changes what *can* be retrieved, so the
  delta may be drift rather than a retrieval change; the caller decides.
- queries with an `error` in either run (empty metrics) are simply not
  shared and drop out of the pairing - `n` says how many survived.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from knowledge_forge.evaluation.ir_metrics import bootstrap_ci, paired_delta
from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery


def _metrics_by_query(rows: Sequence[EvalRunQuery]) -> dict[str, dict[str, float]]:
    return {r.query_id: dict(r.metrics) for r in rows if r.error is None and r.metrics}


def _win_loss_tie(deltas: Sequence[float]) -> dict[str, int]:
    return {
        "wins": sum(1 for d in deltas if d > 0),
        "losses": sum(1 for d in deltas if d < 0),
        "ties": sum(1 for d in deltas if d == 0),
    }


def compare_runs(
    run_a: EvalRun,
    queries_a: Sequence[EvalRunQuery],
    run_b: EvalRun,
    queries_b: Sequence[EvalRunQuery],
    *,
    metric: str = "ndcg@10",
    n_boot: int = 1000,
    seed: int = 0,
) -> dict:
    """B relative to A. Returns a JSON-ready dict: the headline `metric`
    with mean/CI/win-loss-tie/per-query/per-type breakdown, plus a
    `per_metric` table of mean deltas over every metric both runs share."""
    if run_a.golden_version != run_b.golden_version:
        raise ValueError(
            f"cannot compare runs on different golden versions: {run_a.golden_version!r} vs {run_b.golden_version!r}"
        )
    warnings: list[str] = []
    snapshot_mismatch = run_a.index_snapshot != run_b.index_snapshot
    if snapshot_mismatch:
        warnings.append(
            "index_snapshot differs between runs - the delta may reflect index drift, not a retrieval change"
        )
    if run_a.config_hash == run_b.config_hash:
        warnings.append("both runs share config_hash - the delta measures run-to-run noise, not a config change")

    a = _metrics_by_query(queries_a)
    b = _metrics_by_query(queries_b)
    deltas_all = paired_delta(a, b)
    per_metric = {
        name: {k: v for k, v in d.items() if k in ("n", "mean_delta", "wins", "losses", "ties")}
        for name, d in deltas_all.items()
    }

    out: dict = {
        "metric": metric,
        "golden_version": run_a.golden_version,
        "run_a": {"run_id": run_a.run_id, "label": run_a.label, "config_hash": run_a.config_hash},
        "run_b": {"run_id": run_b.run_id, "label": run_b.label, "config_hash": run_b.config_hash},
        "snapshot_mismatch": snapshot_mismatch,
        "warnings": warnings,
        "n_boot": n_boot,
        "seed": seed,
        "per_metric": per_metric,
    }
    head = deltas_all.get(metric)
    if head is None or head["n"] == 0:
        out.update({"n": 0, "mean_a": None, "mean_b": None, "mean_delta": None, "ci95": None})
        out.update({"wins": 0, "losses": 0, "ties": 0, "per_query": [], "by_type": {}})
        return out

    ids: list[str] = head["query_ids"]
    deltas: list[float] = head["deltas"]
    lo, hi = bootstrap_ci(deltas, n_boot=n_boot, seed=seed)
    types_by_id = {r.query_id: list(r.types) for r in queries_b}
    per_query: list[dict[str, Any]] = [
        {"query_id": qid, "a": a[qid][metric], "b": b[qid][metric], "delta": d, "types": types_by_id.get(qid, [])}
        for qid, d in zip(ids, deltas, strict=True)
    ]
    grouped: dict[str, list[float]] = {}
    for qid, d in zip(ids, deltas, strict=True):
        for t in types_by_id.get(qid, []):
            grouped.setdefault(t, []).append(d)
    by_type = {
        t: {"n": len(ds), "mean_delta": sum(ds) / len(ds), **_win_loss_tie(ds)} for t, ds in sorted(grouped.items())
    }
    out.update(
        {
            "n": head["n"],
            "mean_a": sum(a[q][metric] for q in ids) / len(ids),
            "mean_b": sum(b[q][metric] for q in ids) / len(ids),
            "mean_delta": head["mean_delta"],
            "ci95": [lo, hi],
            "significant": not (lo <= 0.0 <= hi),
            "wins": head["wins"],
            "losses": head["losses"],
            "ties": head["ties"],
            "per_query": per_query,
            "by_type": by_type,
        }
    )
    return out
