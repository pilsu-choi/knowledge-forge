"""Shared pipeline-invocation helper for the ingest job worker.

`cli.py`'s `extract` command has its own inline pipeline invocation because
it also owns JSONL/OKF output - concerns the worker doesn't have (a job's
caller reads results back through the read API, not stdout). This module is
the minimal piece actually shared: build the standard Collector/Parser/
Chunker/Extractor stack for a local path or a SaaS source and run it
against a `store`.
"""

from pathlib import Path

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline
from knowledge_forge.storage.store import PipelineStore


async def _run_collector(
    collector: Collector,
    max_tokens: int,
    model: str | None,
    store: PipelineStore | None,
    project_id: str,
) -> dict:
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    extractor = get_extractor(model=model)

    object_count = 0
    relation_count = 0
    async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store, project_id=project_id):
        object_count += len(outcome.objects)
        relation_count += len(outcome.relations)

    return {"object_count": object_count, "relation_count": relation_count}


async def run_local_ingest(
    path: Path | str,
    max_tokens: int,
    model: str | None,
    store: PipelineStore | None,
    project_id: str = "default",
) -> dict:
    """Runs the full pipeline over `path` against `store`, returning a
    JSON-safe summary usable directly as a job's `result`."""
    return await _run_collector(LocalFileCollector(root=path), max_tokens, model, store, project_id)


async def run_saas_ingest(
    source: str,
    max_tokens: int,
    model: str | None,
    store: PipelineStore | None,
    project_id: str = "default",
) -> dict:
    """Same as run_local_ingest, but `source` (e.g. "slack") picks a SaaS
    collector built from KF_* env config (collectors/saas/factory.py) instead
    of a local path - see SAAS_COLLECTOR_BUILDERS. Raises KeyError for an
    unknown `source` (worker._dispatch validates it against the same mapping
    before calling this, so that should not happen in practice) and whatever
    RuntimeError the matching build_*_collector() raises for missing KF_*
    config."""
    collector = SAAS_COLLECTOR_BUILDERS[source]()
    return await _run_collector(collector, max_tokens, model, store, project_id)
