"""Golden-set IR evaluation runner (wiki/search-evaluation.md (C)) - the
glue between `golden.py` (what is relevant), `search/retrieve.py` (what came
back) and `ir_metrics.py` (how good that was), producing one `EvalRun`.

Design points:

- The retriever is **injected** (`retrieve_fn`) rather than imported at
  module level. `search/retrieve.py` is developed in parallel and its
  contract (`RetrieveFn`/`RetrievalResultLike` below) is all this module
  relies on; the default resolves `knowledge_forge.search.retrieve.retrieve`
  lazily so the runner imports (and is unit-testable with a fake) even
  when that module is absent.
- One failing query must not sink the run: retrieval exceptions land in
  that query's `error` column and it contributes no metrics. A 76-query
  run that dies on query 40 because one strategy hit a timeout would
  otherwise leave no usable record at all.
- `limit` is **promoted to 200** for exhaustive/count queries. Those are
  scored on the whole result list (`exhaustive_recall`, `count_match`),
  and a 10-result cap would make a "list every X" query fail by
  construction, not by retrieval quality.
- Stale gold entries (`check_staleness`) are *reported*, not dropped - the
  scores are still computed so a stale run is comparable with the previous
  one, but the summary lists them so a drop can be attributed to drift
  rather than to a retrieval change.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import subprocess
import time
import traceback
import uuid
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from knowledge_forge.evaluation.golden import SPLITS, GoldenQuery, GoldenSet, check_staleness
from knowledge_forge.evaluation.ir_metrics import DEFAULT_K_VALUES, QueryScore, aggregate, score_query
from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery, EvalStore

if TYPE_CHECKING:
    from knowledge_forge.chat_provider import ChatProvider
    from knowledge_forge.storage import PipelineStore

# Result-list size for queries scored on the whole list (exhaustive / count).
EXHAUSTIVE_LIMIT = 200


class RetrievalResultLike(Protocol):
    """The slice of `search.retrieve.RetrievalResult` the runner reads."""

    document_ids: list[str]
    scores: list[float]
    predicted_count: int | None
    strategies: list[str]
    elapsed_ms: float

    # read-only: `RetrievalResult.steps` is `list[RetrievalStep]`, and a mutable
    # protocol attribute would demand an invariant match with `Sequence[Any]`.
    @property
    def steps(self) -> Sequence[Any]: ...


class RetrieveFn(Protocol):
    def __call__(
        self,
        query: str,
        *,
        store: PipelineStore,
        provider: ChatProvider | None,
        project_id: str | None,
        now: datetime | None,
        limit: int,
        use_llm_constraints: bool,
        strategies_override: list[str] | None,
    ) -> Awaitable[RetrievalResultLike]: ...


def default_retrieve_fn() -> RetrieveFn:
    from knowledge_forge.search.retrieve import retrieve

    return retrieve


def config_hash(config: Mapping[str, Any]) -> str:
    """First 12 hex chars of sha256 over the canonical (sorted-key) JSON -
    enough to group runs by settings, short enough to print in a listing."""
    canonical = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def current_git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def effective_limit(q: GoldenQuery, limit: int) -> int:
    if q.expected.exhaustive or q.expected.count is not None:
        return max(limit, EXHAUSTIVE_LIMIT)
    return limit


def select_queries(
    golden: GoldenSet,
    query_filter: set[str] | None = None,
    type_filter: set[str] | None = None,
    split_filter: str | None = None,
) -> list[GoldenQuery]:
    """`type_filter` matches any of a query's (multi-label) `types`, not
    only `primary_type` - asking for `entity_centric` should include the
    relative_temporal query that is *also* entity-centric.

    `split_filter` ("dev"/"test") narrows to one half of a split gold set.
    Asking for a split the file doesn't carry is an error rather than an
    empty run: silently scoring zero queries (or, worse, all of them) is how
    a headline number ends up measured on the wrong population."""
    if split_filter is not None:
        if split_filter not in SPLITS:
            raise ValueError(f"split_filter must be one of {sorted(SPLITS)}, got {split_filter!r}")
        if not golden.has_split():
            raise ValueError(f"golden set {golden.golden_version!r} carries no dev/test split")
    out = []
    for q in golden.queries:
        if query_filter is not None and q.id not in query_filter:
            continue
        if type_filter is not None and not (type_filter & set(q.types)):
            continue
        if split_filter is not None and q.split != split_filter:
            continue
        out.append(q)
    return out


def _steps_to_dicts(steps: Iterable[Any]) -> list[dict]:
    out: list[dict] = []
    for step in steps:
        if dataclasses.is_dataclass(step) and not isinstance(step, type):
            out.append(dataclasses.asdict(step))
        elif isinstance(step, dict):
            out.append(dict(step))
        elif hasattr(step, "to_dict"):
            out.append(step.to_dict())
        else:
            out.append({"repr": repr(step)})
    return out


async def _run_one(
    q: GoldenQuery,
    *,
    run_id: str,
    retrieve_fn: RetrieveFn,
    store: PipelineStore,
    provider: ChatProvider | None,
    project_id: str | None,
    limit: int,
    k_values: Sequence[int],
    strategies_override: list[str] | None,
    use_llm_constraints: bool,
) -> EvalRunQuery:
    row = EvalRunQuery(
        run_id=run_id, query_id=q.id, query_text=q.query, primary_type=q.primary_type, types=list(q.types)
    )
    started = time.perf_counter()
    try:
        result = await retrieve_fn(
            q.query,
            store=store,
            provider=provider,
            project_id=project_id,
            now=q.as_of,
            limit=effective_limit(q, limit),
            use_llm_constraints=use_llm_constraints,
            strategies_override=strategies_override,
        )
        row.ranked_document_ids = list(result.document_ids)
        row.scores = [float(s) for s in result.scores]
        row.predicted_count = result.predicted_count
        row.strategies = list(result.strategies)
        row.steps = _steps_to_dicts(result.steps)
        row.elapsed_ms = float(result.elapsed_ms)
        row.metrics = score_query(
            row.ranked_document_ids,
            q.expected,
            k_values,
            predicted_count=row.predicted_count,
            scores=row.scores or None,
        )
    except Exception as exc:  # noqa: BLE001 - one query's failure must not abort the run
        row.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        row.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return row


async def run_ir_eval(
    golden: GoldenSet,
    *,
    store: PipelineStore,
    eval_store: EvalStore | None,
    golden_path: str = "",
    provider: ChatProvider | None = None,
    provider_name: str | None = None,
    model_name: str | None = None,
    project_id: str | None = None,
    limit: int = 10,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
    strategies_override: list[str] | None = None,
    use_llm_constraints: bool = True,
    label: str | None = None,
    retrieve_fn: RetrieveFn | None = None,
    query_filter: set[str] | None = None,
    type_filter: set[str] | None = None,
    split_filter: str | None = None,
    concurrency: int = 4,
    content_hashes: Mapping[str, str] | None = None,
) -> EvalRun:
    """Score every (selected) golden query and return the finished
    `EvalRun` with `queries` attached. Persisted through `eval_store` when
    one is given (row written at start and again at finish); `None` keeps
    the run in memory only (`eval-ir --no-store`).

    `content_hashes` lets a caller (tests) supply the staleness input
    directly; otherwise it is fetched from `eval_store`, and skipped when
    there is no store at all."""
    fn = retrieve_fn if retrieve_fn is not None else default_retrieve_fn()
    effective_project = golden.project_id or project_id
    queries = select_queries(golden, query_filter, type_filter, split_filter)
    config = {
        "provider": provider_name,
        "model": model_name,
        "limit": limit,
        "strategies_override": list(strategies_override) if strategies_override else None,
        "use_llm_constraints": use_llm_constraints,
        "k_values": list(k_values),
        # 분할이 config에 들어가야 dev run과 test run의 config_hash가 갈린다 -
        # 같아 버리면 `compare_runs`가 "같은 설정끼리 비교 = run-to-run 노이즈"
        # 경고를 내며 서로 다른 모집단을 짝지으려 든다.
        "split": split_filter,
    }
    run = EvalRun(
        run_id=uuid.uuid4().hex,
        golden_version=golden.golden_version,
        golden_path=golden_path,
        project_id=effective_project,
        config=config,
        config_hash=config_hash(config),
        index_snapshot={},
        git_commit=current_git_commit(),
        label=label,
        started_at=datetime.now(timezone.utc),
    )

    if eval_store is not None:
        run.index_snapshot = await eval_store.snapshot_index(effective_project)
        if content_hashes is None:
            wanted = sorted({d for q in queries for d in q.expected.document_ids})
            content_hashes = await eval_store.content_hashes_for(wanted)
        await eval_store.save_eval_run(run)

    stale: list[dict] = []
    if content_hashes is not None:
        subset = golden.model_copy(update={"queries": queries})
        stale = [
            {"query_id": i.query_id, "reason": i.reason, "document_ids": list(i.document_ids)}
            for i in check_staleness(subset, content_hashes)
        ]

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(q: GoldenQuery) -> EvalRunQuery:
        async with semaphore:
            return await _run_one(
                q,
                run_id=run.run_id,
                retrieve_fn=fn,
                store=store,
                provider=provider,
                project_id=effective_project,
                limit=limit,
                k_values=k_values,
                strategies_override=strategies_override,
                use_llm_constraints=use_llm_constraints,
            )

    rows = list(await asyncio.gather(*(_guarded(q) for q in queries)))
    run.queries = rows

    scored: list[QueryScore] = [{"id": r.query_id, "metrics": r.metrics} for r in rows if r.error is None]
    summary = aggregate(scored, golden.types_by_id())
    summary["n_selected"] = len(rows)
    summary["errors"] = [{"query_id": r.query_id, "error": r.error} for r in rows if r.error is not None]
    summary["stale_queries"] = stale
    summary["staleness_checked"] = content_hashes is not None
    run.summary = summary
    run.finished_at = datetime.now(timezone.utc)

    if eval_store is not None:
        await eval_store.save_eval_run_queries(run.run_id, rows)
        await eval_store.save_eval_run(run)
    return run


def run_to_dict(run: EvalRun) -> dict:
    """JSON-ready dump including the (otherwise excluded) query rows - the
    shape `eval-ir --json` writes."""
    payload = run.model_dump(mode="json")
    payload["queries"] = [q.model_dump(mode="json") for q in run.queries]
    return payload
