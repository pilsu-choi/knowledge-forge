"""Orchestrates Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.

This is the one place that wires the stage interfaces together; each stage
stays swappable (a different Collector, Parser registry, Chunker, or
Extractor implementation) without touching this module's logic.
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from knowledge_forge.chunking.base import Chunker
from knowledge_forge.collectors.base import Collector, RawItem
from knowledge_forge.config import settings
from knowledge_forge.context.base import ContextReconstructor, NoopReconstructor
from knowledge_forge.context.registry import default_reconstructor_for
from knowledge_forge.dedup import merge_duplicates
from knowledge_forge.document_links import apply_version_metadata
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


class _DocumentTurns:
    """Admits each item's *document stage* - `record_document` ->
    `link_document` -> OKF resource/`_resolve_links` - strictly in stream
    order, while parsing before it and chunking/embedding/extraction after it
    run concurrently across documents.

    That stage is where an item reads back what earlier items of the same
    run wrote: `_resolve_links` needs their endpoints, `link_document` their
    `documents` rows (THREAD_SEQUENCE/SAME_CHANNEL_SEQUENCE targets, a Slack
    thread's parent episode) and a version family's current members, whose
    PREVIOUS_VERSION chain it deletes and rebuilds (two documents of one
    family relinking at once could otherwise delete each other's edges).
    Ordering the stage keeps all of that exactly as in the sequential loop
    without per-link-type bookkeeping. Every item calls `finish` on every
    path - one that never reaches its turn (skipped, failed to parse) just
    releases it."""

    def __init__(self) -> None:
        self._next = 0
        self._finished: set[int] = set()
        self._waiters: dict[int, asyncio.Future[None]] = {}

    async def wait(self, index: int) -> None:
        if index != self._next:
            self._waiters[index] = asyncio.get_running_loop().create_future()
            await self._waiters[index]

    def finish(self, index: int) -> None:
        if index < self._next:
            return
        self._finished.add(index)
        while self._next in self._finished:
            self._finished.remove(self._next)
            self._next += 1
            waiter = self._waiters.pop(self._next, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(None)


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
    force: bool = False,
) -> AsyncIterator[ExtractionOutcome]:
    """Run the full pipeline over everything a Collector yields.

    Yields one `ExtractionOutcome` (its KnowledgeObjects + the Relations
    connecting them) per document - not a single KnowledgeObject per chunk,
    since a chunk can extract into zero, one, or several typed objects (see
    extraction/base.py and models/knowledge_object.py), and a document's
    chunk groups are merged into one outcome before yielding (see the dedup/
    supersedes paragraph below).

    Up to `settings.max_concurrent_documents` (`KF_MAX_CONCURRENT_DOCUMENTS`,
    default 4) items are processed at once; outcomes are still yielded in
    stream order, and each item's document stage (record_document ->
    link_document -> OKF links) runs in stream order too, so everything that
    reads back earlier items of the run sees exactly what the sequential
    loop did (see `_DocumentTurns`). LLM/VLM/embedding calls stay bounded by
    each provider's own `KF_MAX_CONCURRENT_LLM_REQUESTS` semaphore.

    Items a Parser doesn't support (UnsupportedFormatError/NotImplementedError),
    that fail extraction (KnowledgeExtractionError), or whose embedding call
    fails (e.g. a provider rejecting an oversized request) are logged and
    skipped rather than aborting the whole run - one bad file shouldn't block
    the rest of a batch. An embedding failure only skips that document's
    embedding step; its Document/Chunks (already written by the time
    embedding runs) are kept.

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
    a no-op for File/Notion/Google Drive sources - or to a `NoopReconstructor`
    outright when `collector.already_reconstructed` is set (collectors/
    replay.py), since a replayed item is already the reconstructor's output.
    A reconstructor can also
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

    Within one document: chunks are bundled into groups of up to
    `settings.extraction_group_max_tokens` total tokens before extraction
    (see extraction/grouping.py), each group's near-duplicate objects are merged
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

    When `store` is given and `force` is false (the default), an item whose
    `stable_id(source_type, uri)` already has a `documents` row with a
    matching `content_hash` is unchanged since the last successful run and
    is skipped entirely - no re-parse, re-chunk, LLM re-extraction, or
    re-embedding, and no `ExtractionOutcome` is yielded for it (every
    caller only counts outcomes/objects it receives, so this shrinks those
    counts rather than miscounting them). The check runs before
    `record_source()` so a skip truly writes nothing. `force=True` (what
    `reparse`'s replay collector passes, see cli.py) bypasses the check -
    replay's whole purpose is re-parsing unchanged bytes with newer parser
    code.
    """
    # collector.already_reconstructed (collectors/base.py) means every item
    # this collector yields is already the semantic unit a reconstructor
    # would build (collectors/replay.py: replayed episodes/threads) -
    # running e.g. SlackConversationReconstructor over one again is not
    # idempotent (see that flag's docstring), so it is skipped entirely
    # rather than defaulted to.
    reconstructor = context_reconstructor or (
        NoopReconstructor() if collector.already_reconstructed else default_reconstructor_for(collector.source_type)
    )
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
    turns = _DocumentTurns()
    link_endpoints_by_uri: dict[str, LinkEndpoint] = {}

    async def _process(index: int, item: RawItem) -> tuple[ExtractionOutcome | None, BaseException | None]:
        """One item end to end. Returns (outcome to yield or None, exception
        to re-raise after yielding it) - raising directly would lose the
        outcome of a document whose extraction partly succeeded."""
        try:
            source = _make_source(collector, item, project_id)
            source_ref = SourceRef.from_source(source)

            if store is not None and not force:
                existing_hash = await store.get_source_content_hash(source.source_id)
                if existing_hash is not None and existing_hash == source.content_hash:
                    logger.info("skipping unchanged source (content_hash matches an existing document): %s", item.uri)
                    return None, None

            if store is not None:
                await store.record_source(source, item)

            try:
                parser = parser_registry.get_parser_for(item.uri, filename=item.metadata.get("name"))
                document = await parser.parse(item, source_ref)
            except NotImplementedError as exc:
                logger.warning("no working parser for %s, skipping: %s", item.uri, exc)
                return None, None
            except Exception as exc:  # noqa: BLE001 - a bad file must not abort the run
                logger.error("failed to parse %s, skipping: %s", item.uri, exc)
                return None, None
            # Version metadata is parsed from the name before the row is
            # written so documents.metadata carries it; the links are built
            # after, since they read the family / uri->document map back from
            # the store (document_links.py).
            apply_version_metadata(document, source)

            await turns.wait(index)
            if store is not None:
                await store.record_document(document)
                await store.link_document(source, document)
            if okf_writer is not None:
                okf_writer.write_resource(source, document)
            current_endpoint = LinkEndpoint(source_id=source.source_id, document_id=document.document_id)
            if okf_writer is not None:
                links = _resolve_links(item, current_endpoint, link_endpoints_by_uri)
                if links:
                    okf_writer.write_links(links, source.project_id)
            link_endpoints_by_uri[item.uri] = current_endpoint
            turns.finish(index)

            chunks = chunker.chunk(document)
            if store is not None:
                await store.record_chunks(document.document_id, chunks)
                if embed_chunks:
                    assert embedder is not None
                    try:
                        await store.record_chunk_embeddings(
                            chunks, source.project_id, embedder, settings.embedding_model
                        )
                    except Exception as exc:  # noqa: BLE001 - one document's embedding failure must not abort the run
                        logger.error("failed to embed chunks for %s, skipping embedding: %s", document.document_id, exc)

            if extractor is None:
                return ExtractionOutcome(objects=[], relations=[]), None

            chunk_groups = group_chunks(chunks, settings.extraction_group_max_tokens)

            async def _extract(group):
                try:
                    return await extractor.extract(group, document, source)
                except KnowledgeExtractionError as exc:
                    group_ids = ",".join(c.chunk_id for c in group)
                    logger.error("extraction failed for chunk(s) %s, skipping: %s", group_ids, exc)
                    return None

            # Chunk groups within one document are extracted concurrently
            # (bounded by the Extractor's own semaphore, shared by every
            # document in the run); `return_exceptions=True` so one group's
            # unexpected failure doesn't strand its siblings' results - they
            # still feed into the merge/yield below before the exception is
            # re-raised.
            results = await asyncio.gather(*(_extract(g) for g in chunk_groups), return_exceptions=True)
            unexpected = next((r for r in results if isinstance(r, BaseException)), None)
            valid_outcomes = [r for r in results if r is not None and not isinstance(r, BaseException)]
            if not valid_outcomes:
                return None, unexpected

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

            merged_outcome = ExtractionOutcome(objects=deduped.objects, relations=deduped.relations + supersede_relations)
            if store is not None:
                await store.record_knowledge_objects(merged_outcome.objects)
                await store.record_relations(merged_outcome.relations)
                if embed_objects:
                    assert embedder is not None
                    try:
                        await store.record_embeddings(merged_outcome.objects, embedder, settings.embedding_model)
                    except Exception as exc:  # noqa: BLE001 - one document's embedding failure must not abort the run
                        logger.error(
                            "failed to embed knowledge objects for %s, skipping embedding: %s",
                            document.document_id,
                            exc,
                        )
            if okf_writer is not None:
                okf_writer.write_objects(merged_outcome.objects)
                okf_writer.write_relations(merged_outcome.relations, source.project_id)
            return merged_outcome, unexpected
        finally:
            turns.finish(index)

    # Up to `max_concurrent_documents` items in flight; outcomes are still
    # yielded in stream order (a finished item waits for every earlier one).
    # The first exception is raised at its item's place in that order, after
    # which the items still in flight are cancelled - they may have written
    # part of their rows already, unlike the sequential loop where nothing
    # after the failing item had started.
    concurrency = max(1, settings.max_concurrent_documents)
    running: set[asyncio.Task] = set()
    in_order: deque[asyncio.Task] = deque()
    try:
        index = 0
        for item in reconstructor.reconstruct(items):
            if item.deleted:
                logger.info("skipping deleted item: %s", item.uri)
                continue
            if len(running) >= concurrency:
                _, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.create_task(_process(index, item))
            index += 1
            running.add(task)
            in_order.append(task)
            while in_order and in_order[0].done():
                outcome, unexpected = in_order.popleft().result()
                if outcome is not None:
                    yield outcome
                if unexpected is not None:
                    raise unexpected
        while in_order:
            outcome, unexpected = await in_order.popleft()
            if outcome is not None:
                yield outcome
            if unexpected is not None:
                raise unexpected
    finally:
        for task in in_order:
            task.cancel()
        await asyncio.gather(*in_order, return_exceptions=True)
