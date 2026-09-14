"""Shared pipeline-invocation helper for the ingest job worker.

`cli.py`'s `extract` command has its own inline pipeline invocation because
it also owns JSONL output - a concern the worker doesn't have (a job's
caller reads results back through the read API, not stdout). OKF output
*is* shared here (see `okf_writer` below) - a job is a one-shot run just
like `extract`, so the same "write manifest/views once, at the end" timing
applies (unlike `schedule`'s per-tick refresh, see scheduling/scheduler.py).
This module is the minimal piece actually shared: build the standard
Collector/Parser/Chunker/Extractor stack for a local path or a SaaS source
and run it against a `store`.
"""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, Cursor
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS
from knowledge_forge.config import settings
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline
from knowledge_forge.storage.store import PipelineStore

if TYPE_CHECKING:
    from knowledge_forge.okf import OKFWriter


async def _run_collector(
    collector: Collector,
    max_tokens: int,
    model: str | None,
    store: PipelineStore | None,
    project_id: str,
    okf_writer: "OKFWriter | None" = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: Cursor | None = None,
) -> dict:
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens, min_tokens=settings.min_tokens_per_chunk)
    extractor = get_extractor(model=model)

    object_count = 0
    relation_count = 0
    async for outcome in run_pipeline(
        collector,
        registry,
        chunker,
        extractor,
        cursor=cursor,
        store=store,
        okf_writer=okf_writer,
        project_id=project_id,
        since=since,
        until=until,
    ):
        object_count += len(outcome.objects)
        relation_count += len(outcome.relations)

    if okf_writer is not None:
        okf_writer.write_manifest()
        okf_writer.write_views()

    return {"object_count": object_count, "relation_count": relation_count}


async def run_local_ingest(
    path: Path | str,
    max_tokens: int,
    model: str | None,
    store: PipelineStore | None,
    project_id: str = "default",
    okf_writer: "OKFWriter | None" = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    """Runs the full pipeline over `path` against `store`, returning a
    JSON-safe summary usable directly as a job's `result`. `since`/`until`
    (opt-in, from an /ingest request's backfill fields) re-collect only that
    date/time window - see run_pipeline's docstring."""
    return await _run_collector(
        LocalFileCollector(root=path), max_tokens, model, store, project_id, okf_writer, since, until
    )


async def run_saas_ingest(
    source: str,
    max_tokens: int,
    model: str | None,
    store: PipelineStore | None,
    project_id: str = "default",
    okf_writer: "OKFWriter | None" = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    """Same as run_local_ingest, but `source` (e.g. "slack") picks a SaaS
    collector built from KF_* env config (collectors/saas/factory.py) instead
    of a local path - see SAAS_COLLECTOR_BUILDERS. Raises KeyError for an
    unknown `source` (worker._dispatch validates it against the same mapping
    before calling this, so that should not happen in practice) and whatever
    RuntimeError the matching build_*_collector() raises for missing KF_*
    config.

    Unlike run_local_ingest, this persists an incremental-sync cursor across
    calls (`store.get_sync_cursor`/`save_sync_cursor`, one row per `source`
    - see schema.sql's sync_cursors table): without it, every job-queue
    ingest of the same source called `Collector.sync(cursor=None)` ->
    `initial_sync()` (collectors/base.py), re-collecting and re-extracting a
    SaaS source's *entire* history on every single run - fine for a one-off
    `extract`/`schedule` CLI invocation, ruinous for KF_SCHEDULED_SOURCES
    firing every few minutes forever. `since` (a backfill request, see
    run_pipeline's docstring) is mutually exclusive with the persisted
    cursor by design - a backfill re-collects a specific window without
    touching or being affected by the normal incremental position.
    """
    collector = SAAS_COLLECTOR_BUILDERS[source]()

    cursor = None
    if store is not None and since is None:
        cursor_data = await store.get_sync_cursor(source)
        if cursor_data is not None:
            cursor = Cursor(data=cursor_data)

    result = await _run_collector(collector, max_tokens, model, store, project_id, okf_writer, since, until, cursor)

    if store is not None and since is None:
        await store.save_sync_cursor(source, collector.get_cursor().data)

    return result
