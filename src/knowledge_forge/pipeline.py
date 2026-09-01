"""Orchestrates Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.

This is the one place that wires the stage interfaces together; each stage
stays swappable (a different Collector, Parser registry, Chunker, or
Extractor implementation) without touching this module's logic.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator

from knowledge_forge.chunking.base import Chunker
from knowledge_forge.collectors.base import Collector, RawItem
from knowledge_forge.context.base import ContextReconstructor
from knowledge_forge.context.registry import default_reconstructor_for
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.ids import stable_id
from knowledge_forge.models.source import Source, SourceRef
from knowledge_forge.parsers.registry import ParserRegistry

if TYPE_CHECKING:
    from knowledge_forge.okf import OKFWriter
    from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)


def _make_source(collector: Collector, item: RawItem) -> Source:
    return Source(
        source_id=stable_id(collector.source_type.value, item.uri),
        source_type=collector.source_type,
        uri=item.uri,
        content_hash=item.content_hash,
        collected_at=datetime.now(timezone.utc),
        modified_at=item.modified_at,
        metadata=item.metadata,
    )


async def run_pipeline(
    collector: Collector,
    parser_registry: ParserRegistry,
    chunker: Chunker,
    extractor: Extractor,
    cursor=None,
    store: "PipelineStore | None" = None,
    context_reconstructor: ContextReconstructor | None = None,
    okf_writer: "OKFWriter | None" = None,
) -> AsyncIterator[ExtractionOutcome]:
    """Run the full pipeline over everything a Collector yields.

    Yields one `ExtractionOutcome` (its KnowledgeObjects + the Relations
    connecting them) per chunk - not a single KnowledgeObject per chunk, since
    a chunk can extract into zero, one, or several typed objects (see
    extraction/base.py and models/knowledge_object.py).

    Items a Parser doesn't support (UnsupportedFormatError/NotImplementedError)
    or that fail extraction (KnowledgeExtractionError) are logged and skipped
    rather than aborting the whole run - one bad file shouldn't block the rest
    of a batch.

    When `store` is given, each stage's artifact is persisted as it is
    produced (raw bytes + Source to disk/Postgres, then Document, then
    Chunks, then each chunk's KnowledgeObjects/Relations) - `store=None`
    (the default) runs exactly as before, print-only, with no persistence
    side effects.

    When `okf_writer` is given, the same artifacts are additionally written
    into a file-based OKF Knowledge Base tree (see knowledge_forge.okf) -
    independent of `store`, since OKF is a portable file-tree view of the
    same data, not a replacement for the queryable Postgres store. The
    caller is responsible for calling `okf_writer.write_manifest()` once
    after this generator is exhausted.

    Before parsing, `context_reconstructor` re-groups the Collector's raw
    item stream into semantically complete units (see knowledge_forge.context)
    - a Slack message or Gmail email in isolation is often incomplete.
    Defaults to `default_reconstructor_for(collector.source_type)`, which is
    a no-op for File/Notion/Google Drive sources.
    """
    reconstructor = context_reconstructor or default_reconstructor_for(collector.source_type)
    for item in reconstructor.reconstruct(collector.sync(cursor)):
        if item.deleted:
            logger.info("skipping deleted item: %s", item.uri)
            continue

        source = _make_source(collector, item)
        source_ref = SourceRef.from_source(source)
        if store is not None:
            await store.record_source(source, item)

        try:
            parser = parser_registry.get_parser_for(item.uri)
            document = await parser.parse(item, source_ref)
        except NotImplementedError as exc:
            logger.warning("no working parser for %s, skipping: %s", item.uri, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - a bad file must not abort the run
            logger.error("failed to parse %s, skipping: %s", item.uri, exc)
            continue
        if store is not None:
            await store.record_document(document)
        if okf_writer is not None:
            okf_writer.write_resource(source, document)

        chunks = chunker.chunk(document)
        if store is not None:
            await store.record_chunks(chunks)

        async def _extract(chunk):
            try:
                return await extractor.extract(chunk, document, source)
            except KnowledgeExtractionError as exc:
                logger.error("extraction failed for chunk %s, skipping: %s", chunk.chunk_id, exc)
                return None

        # Chunks within one document are extracted concurrently (bounded by
        # the Extractor's own semaphore); `return_exceptions=True` so one
        # chunk's unexpected failure doesn't strand its siblings' results -
        # they're still yielded/stored before the exception is re-raised.
        results = await asyncio.gather(*(_extract(c) for c in chunks), return_exceptions=True)
        unexpected = next((r for r in results if isinstance(r, BaseException)), None)
        for outcome in results:
            if outcome is None or isinstance(outcome, BaseException):
                continue
            if store is not None:
                await store.record_knowledge_objects(outcome.objects)
                await store.record_relations(outcome.relations)
            if okf_writer is not None:
                okf_writer.write_objects(outcome.objects)
                okf_writer.write_relations(outcome.relations)
            yield outcome
        if unexpected is not None:
            raise unexpected
