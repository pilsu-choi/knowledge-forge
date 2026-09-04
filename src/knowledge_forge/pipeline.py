"""Orchestrates Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.

This is the one place that wires the stage interfaces together; each stage
stays swappable (a different Collector, Parser registry, Chunker, or
Extractor implementation) without touching this module's logic.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from knowledge_forge.chunking.base import Chunker
from knowledge_forge.collectors.base import Collector, RawItem
from knowledge_forge.config import settings
from knowledge_forge.context.base import ContextReconstructor
from knowledge_forge.context.registry import default_reconstructor_for
from knowledge_forge.dedup import merge_duplicates
from knowledge_forge.embedding.base import EmbeddingProvider
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.extraction.grouping import group_chunks
from knowledge_forge.extraction.versioning import resolve_supersedes
from knowledge_forge.ids import stable_id
from knowledge_forge.models.link import Link, LinkEndpoint, LinkType
from knowledge_forge.models.source import Source, SourceRef
from knowledge_forge.parsers.registry import ParserRegistry

if TYPE_CHECKING:
    from knowledge_forge.okf import OKFWriter
    from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)


def _make_source(collector: Collector, item: RawItem, project_id: str) -> Source:
    return Source(
        source_id=stable_id(collector.source_type.value, item.uri),
        source_type=collector.source_type,
        uri=item.uri,
        content_hash=item.content_hash,
        collected_at=datetime.now(timezone.utc),
        modified_at=item.modified_at,
        metadata=item.metadata,
        project_id=item.project_id or project_id,
    )


def _resolve_links(
    item: RawItem, current: LinkEndpoint, endpoint_by_uri: dict[str, LinkEndpoint]
) -> list[Link]:
    """Materializes `Link` records for the resource-level edges a
    ContextReconstructor annotated on `item.metadata["links"]` (see
    context/slack.py) - each entry names a `type` and the `target_uri` of an
    earlier-yielded item in this same run, resolved here against every
    prior item's already-known (source_id, document_id) endpoint. An entry
    whose target was never seen (e.g. it failed parsing and got skipped) is
    silently dropped - there is nothing to point the edge at."""
    links = []
    for entry in item.metadata.get("links", []):
        target = endpoint_by_uri.get(entry["target_uri"])
        if target is None:
            continue
        links.append(
            Link(
                link_id=stable_id("link", entry["type"], target.source_id, current.source_id),
                type=LinkType(entry["type"]),
                source_ref=target,
                target_ref=current,
                created_at=datetime.now(timezone.utc),
            )
        )
    return links


async def run_pipeline(
    collector: Collector,
    parser_registry: ParserRegistry,
    chunker: Chunker,
    extractor: Extractor | None,
    cursor=None,
    store: "PipelineStore | None" = None,
    context_reconstructor: ContextReconstructor | None = None,
    okf_writer: "OKFWriter | None" = None,
    project_id: str = "default",
    since: datetime | None = None,
    until: datetime | None = None,
    embedder: EmbeddingProvider | None = None,
    embedding_source: str | None = None,
) -> AsyncIterator[ExtractionOutcome]:
    """Run the full pipeline over everything a Collector yields.

    Yields one `ExtractionOutcome` (its KnowledgeObjects + the Relations
    connecting them) per document - not a single KnowledgeObject per chunk,
    since a chunk can extract into zero, one, or several typed objects (see
    extraction/base.py and models/knowledge_object.py), and a document's
    chunk groups are merged into one outcome before yielding (see the dedup/
    supersedes paragraph below).

    Items a Parser doesn't support (UnsupportedFormatError/NotImplementedError)
    or that fail extraction (KnowledgeExtractionError) are logged and skipped
    rather than aborting the whole run - one bad file shouldn't block the rest
    of a batch.

    When `store` is given, each stage's artifact is persisted as it is
    produced (raw bytes + Source to disk/Postgres, then Document, then
    Chunks, then each document's merged KnowledgeObjects/Relations) -
    `store=None` (the default) runs exactly as before, print-only, with no
    persistence side effects.

    When `embedder` is also given (requires `store` - there is nowhere to
    write embeddings otherwise), what gets embedded depends on
    `embedding_source` (default `settings.embedding_source`,
    `KF_EMBEDDING_SOURCE`, see wiki/embeddings.md "청크 기반 vs
    KnowledgeObject 기반"): `"knowledge_object"` embeds each document's
    merged, deduped, final KnowledgeObjects right after
    `store.record_knowledge_objects()`; `"chunk"` embeds the raw chunks
    right after `store.record_chunks()` - i.e. *before* extraction, so the
    document is vector-searchable as soon as it is chunked, whether or not
    extraction runs at all. `embedder=None` (the default) skips embedding
    entirely, same opt-in shape as `store`.

    `extractor=None` skips Knowledge Extraction altogether: the document is
    still collected, parsed, chunked, stored and (in chunk mode) embedded,
    and an empty `ExtractionOutcome` is yielded for it so callers still see
    one outcome per processed document. This is the "KnowledgeObjects are
    optional" path for sources where LLM extraction is too slow/expensive to
    gate retrieval on - pair it with `embedding_source="chunk"`, otherwise
    nothing gets embedded (there are no KOs to embed).

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
    a no-op for File/Notion/Google Drive sources. A reconstructor can also
    annotate a RawItem's `metadata["links"]` with edges to earlier-yielded
    items in the same run (see context/slack.py) - `_resolve_links()` turns
    those into `Link` records once both endpoints' Source/Document exist,
    written via `okf_writer.write_links()` (wiki/okf.md's "그래프 뷰어 대비"
    decision: these are resource-to-resource edges `Relation` can't express).

    `project_id` (default "default") tags every Source this run collects,
    unless the RawItem itself carries a `project_id` (e.g. SlackCollector's
    per-channel `channel_project_map`, see collectors/saas/slack.py), which
    takes precedence - this is what lets one collector instance spanning
    several projects (several Slack channels, each owned by a different
    project) still tag each item correctly. The tenancy key `sources`/
    `documents` are filtered by in the read API (wiki/api.md) and job queue
    (`jobs.project_id`).

    Within one document: chunks are bundled into groups of
    `settings.extraction_chunk_group_size` before extraction (see
    extraction/grouping.py), each group's near-duplicate objects are merged
    (extraction/versioning.py counterpart, dedup.py) at
    `settings.dedup_similarity_threshold`, and - when `store` is given, so
    there is something to compare against - new objects are matched to the
    same source's prior objects to auto-fill `supersedes`/`version`
    (extraction/versioning.py) at `settings.supersedes_similarity_threshold`.

    `since` (mutually exclusive with `cursor` in practice - when given, the
    item stream is `collector.backfill_sync(since, until)` instead of
    `collector.sync(cursor)`) re-collects a specific date/time window
    independent of the collector's incremental cursor - for recovering data
    missed while scheduled sync wasn't running (see cli.py's `backfill`
    command and collectors/base.py's `Collector.backfill_sync`). Everything
    downstream (storage, dedup, supersedes, OKF writing) runs exactly as for
    a normal sync - backfill is just a different RawItem source.
    """
    reconstructor = context_reconstructor or default_reconstructor_for(collector.source_type)
    items = collector.backfill_sync(since, until) if since is not None else collector.sync(cursor)
    embedding_source = embedding_source or settings.embedding_source
    if embedding_source not in ("knowledge_object", "chunk"):
        raise ValueError(f"unknown embedding_source: {embedding_source!r}")
    embed_chunks = embedder is not None and store is not None and embedding_source == "chunk"
    embed_objects = embedder is not None and store is not None and embedding_source == "knowledge_object"
    if extractor is None and embedder is not None and not embed_chunks:
        logger.warning(
            "extractor=None with embedding_source=%r - nothing will be embedded (no KnowledgeObjects); "
            "use embedding_source='chunk' to embed the raw chunks instead",
            embedding_source,
        )
    link_endpoints_by_uri: dict[str, LinkEndpoint] = {}
    for item in reconstructor.reconstruct(items):
        if item.deleted:
            logger.info("skipping deleted item: %s", item.uri)
            continue

        source = _make_source(collector, item, project_id)
        source_ref = SourceRef.from_source(source)
        if store is not None:
            await store.record_source(source, item)

        try:
            parser = parser_registry.get_parser_for(item.uri, filename=item.metadata.get("name"))
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

        current_endpoint = LinkEndpoint(source_id=source.source_id, document_id=document.document_id)
        if okf_writer is not None:
            links = _resolve_links(item, current_endpoint, link_endpoints_by_uri)
            if links:
                okf_writer.write_links(links, source.project_id)
        link_endpoints_by_uri[item.uri] = current_endpoint

        chunks = chunker.chunk(document)
        if store is not None:
            await store.record_chunks(chunks)
            if embed_chunks:
                assert embedder is not None
                await store.record_chunk_embeddings(chunks, source.project_id, embedder, settings.embedding_model)

        if extractor is None:
            yield ExtractionOutcome(objects=[], relations=[])
            continue

        chunk_groups = group_chunks(chunks, settings.extraction_chunk_group_size)

        async def _extract(group):
            try:
                # B023 false positive: awaited via gather() below within this same loop iteration, never deferred past it
                return await extractor.extract(group, document, source)  # noqa: B023
            except KnowledgeExtractionError as exc:
                group_ids = ",".join(c.chunk_id for c in group)
                logger.error("extraction failed for chunk(s) %s, skipping: %s", group_ids, exc)
                return None

        # Chunk groups within one document are extracted concurrently
        # (bounded by the Extractor's own semaphore); `return_exceptions=True`
        # so one group's unexpected failure doesn't strand its siblings'
        # results - they still feed into the merge/yield below before the
        # exception is re-raised.
        results = await asyncio.gather(*(_extract(g) for g in chunk_groups), return_exceptions=True)
        unexpected = next((r for r in results if isinstance(r, BaseException)), None)
        valid_outcomes = [r for r in results if r is not None and not isinstance(r, BaseException)]

        if valid_outcomes:
            # Dedup/supersedes both need to see every object this document
            # produced at once (a duplicate can straddle two chunk groups;
            # a re-extraction's prior version is unknown group-by-group) -
            # so groups are merged into one ExtractionOutcome per document
            # rather than yielded/stored per group as before.
            objects = [ko for outcome in valid_outcomes for ko in outcome.objects]
            relations = [rel for outcome in valid_outcomes for rel in outcome.relations]
            deduped = merge_duplicates(objects, relations, threshold=settings.dedup_similarity_threshold)

            supersede_relations: list = []
            if store is not None:
                prior_objects = await store.list_prior_knowledge_objects(source.source_id)
                supersede_relations = resolve_supersedes(
                    deduped.objects, prior_objects, threshold=settings.supersedes_similarity_threshold
                )

            merged_outcome = ExtractionOutcome(
                objects=deduped.objects, relations=deduped.relations + supersede_relations
            )
            if store is not None:
                await store.record_knowledge_objects(merged_outcome.objects)
                await store.record_relations(merged_outcome.relations)
                if embed_objects:
                    assert embedder is not None
                    await store.record_embeddings(merged_outcome.objects, embedder, settings.embedding_model)
            if okf_writer is not None:
                okf_writer.write_objects(merged_outcome.objects)
                okf_writer.write_relations(merged_outcome.relations, source.project_id)
            yield merged_outcome
        if unexpected is not None:
            raise unexpected
