"""Shared pipeline-invocation helper for the ingest job worker.

`cli.py`'s `extract` command has its own inline pipeline invocation because
it also owns JSONL/OKF output - concerns the worker doesn't have (a job's
caller reads results back through the read API, not stdout). This module is
the minimal piece actually shared: build the standard Collector/Parser/
Chunker/Extractor stack for a local path and run it against a `store`.
"""

from pathlib import Path

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline
from knowledge_forge.storage.store import PipelineStore


async def run_local_ingest(path: Path | str, max_tokens: int, model: str | None, store: PipelineStore | None) -> dict:
    """Runs the full pipeline over `path` against `store`, returning a
    JSON-safe summary usable directly as a job's `result`."""
    collector = LocalFileCollector(root=path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    extractor = get_extractor(model=model)

    object_count = 0
    relation_count = 0
    async for outcome in run_pipeline(collector, registry, chunker, extractor, store=store):
        object_count += len(outcome.objects)
        relation_count += len(outcome.relations)

    return {"object_count": object_count, "relation_count": relation_count}
