"""Document version metadata + document-to-document links at ingest.

Two of the search types wiki/search-architecture.md lists - Version
("최종본/3차 수정본 보여줘") and Parent/Child ("이 메일의 첨부파일", "이
스레드 앞 대화") - can't be resolved at query time: the signal is in file
names and source metadata, not in document content. This module captures
it once, when a Document is stored, as

  * `DocumentMetadata.version_*` (parsed by ingest_version.py from the
    file name / mail subject), and
  * rows in the `document_links` table (models/link.py's DocumentLink).

`pipeline.run_pipeline` calls `apply_version_metadata` before
`store.record_document` (so the JSONB row carries the version keys) and
`PipelineStore.link_document` right after it; `knowledge-forge db
backfill-links` runs `backfill_document_versions_and_links` over rows that
predate this module. Both paths share the same two builders below so an
ingest-time link and a backfilled link are byte-identical (same stable
link_id).

Which link types are actually produced today, and why the others are
schema-only, is recorded in wiki/storage.md "document_links".
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from knowledge_forge.ids import stable_id
from knowledge_forge.ingest_version import VersionInfo, parse_version, version_sort_key
from knowledge_forge.models.document import Document
from knowledge_forge.models.link import DocumentLink, LinkType
from knowledge_forge.models.source import Source, SourceType

if TYPE_CHECKING:
    # pipeline.py imports `apply_version_metadata` from here and must stay
    # importable without a database driver in play (store=None runs).
    from knowledge_forge.storage.postgres_store import PostgresStore

logger = logging.getLogger(__name__)

_BACKFILL_PAGE_SIZE = 500


def display_name(source: Source, document: Document | None = None) -> str | None:
    """The human-given name a version marker would live in: Drive's file
    name, a local file's name, a mail thread's subject. Slack episodes have
    no name (their uri is a channel + timestamp) and are deliberately left
    unversioned - a channel's episodes are a timeline, not revisions of one
    document. Falls back to the parser's own title for anything else."""
    metadata = source.metadata
    if source.source_type == SourceType.SLACK:
        return None
    if source.source_type == SourceType.GOOGLE_DRIVE:
        name = metadata.get("name")
    elif source.source_type == SourceType.GMAIL:
        name = metadata.get("subject")
    elif source.source_type == SourceType.LOCAL_FILE:
        name = metadata.get("filename") or unquote(urlparse(source.uri).path.rsplit("/", 1)[-1])
    else:
        name = metadata.get("name") or metadata.get("title")
    if not name and document is not None:
        name = document.metadata.title
    return name or None


def apply_version_metadata(document: Document, source: Source) -> VersionInfo | None:
    """Fills `document.metadata.version_*` in place from the source's
    display name and returns what was parsed (None when there is no name
    to parse, in which case the fields are left as they were)."""
    info = parse_version(display_name(source, document))
    if info is None:
        return None
    document.metadata.version_label = info.label
    document.metadata.version_number = info.number
    document.metadata.is_final = info.is_final
    document.metadata.version_family = info.family_key
    return info


def _version_patch(info: VersionInfo) -> dict[str, Any]:
    return {
        "version_label": info.label,
        "version_number": info.number,
        "is_final": info.is_final,
        "version_family": info.family_key,
    }


def _make_link(
    link_type: LinkType,
    from_document_id: str,
    to_document_id: str,
    *,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> DocumentLink:
    return DocumentLink(
        link_id=stable_id("document_link", link_type.value, from_document_id, to_document_id),
        from_document_id=from_document_id,
        to_document_id=to_document_id,
        link_type=link_type,
        confidence=confidence,
        metadata=metadata or {},
        created_at=created_at or datetime.now(timezone.utc),
    )


def _version_key(document: Document) -> tuple[int, int, float, str, str]:
    meta = document.metadata
    return version_sort_key(meta.version_label, meta.version_number, meta.is_final)


async def relink_version_family(db: PostgresStore, family_key: str, project_id: str) -> list[DocumentLink]:
    """Rebuilds the PREVIOUS_VERSION chain of one family: every member's
    existing PREVIOUS_VERSION edges are dropped and the chain is re-derived
    from the current ordering (`list_version_family`), so ingesting v2
    after v3 already exists re-points v3 -> v2 instead of leaving v3 -> v1
    behind.

    A pair is only linked when the two sides carry *different* version
    keys. Two uploads with the same name and marker (`image.png` x65 in the
    dev database, `routing_sample.xlsx` twice, "주간보고 v1" and "Re: 주간보고
    v1") are copies of one version, not successive versions - chaining
    them would manufacture 60+ bogus "이전 버전" answers. An unmarked
    original under a marked one ("보고서.docx" -> "보고서_v2.docx") does get
    linked, since that *is* the common way a first version gets named."""
    members = await db.list_version_family(family_key, project_id)
    await db.delete_document_links([d.document_id for d in members], LinkType.PREVIOUS_VERSION.value)
    links: list[DocumentLink] = []
    for older, newer in zip(members, members[1:], strict=False):
        if _version_key(older) == _version_key(newer):
            continue
        links.append(
            _make_link(
                LinkType.PREVIOUS_VERSION,
                newer.document_id,
                older.document_id,
                metadata={
                    "version_family": family_key,
                    "from_version": newer.metadata.version_label,
                    "to_version": older.metadata.version_label,
                },
            )
        )
    await db.save_document_links(links)
    return links


async def build_source_links(db: PostgresStore, source: Source, document: Document) -> list[DocumentLink]:
    """Edges that come from the Source's own metadata rather than from its
    name: the `links: [{type, target_uri}]` entries a ContextReconstructor
    annotated (SAME_CHANNEL_SEQUENCE / THREAD_SEQUENCE - the same entries
    pipeline._resolve_links turns into OKF `Link`s, resolved here through
    the store instead of the in-run uri map so a target ingested in an
    earlier run still resolves), plus THREAD_PARENT for a Slack thread
    episode whose parent message lives in a separately stored timeline
    episode."""
    project_id = source.project_id
    links: list[DocumentLink] = []

    entries = [e for e in source.metadata.get("links", []) if isinstance(e, dict) and e.get("target_uri")]
    targets = await db.find_document_ids_by_source_uris([e["target_uri"] for e in entries], project_id)
    for entry in entries:
        target_document_id = targets.get(entry["target_uri"])
        if target_document_id is None or target_document_id == document.document_id:
            continue
        try:
            link_type = LinkType(entry["type"])
        except ValueError:
            logger.warning("unknown link type %r on %s, skipping", entry.get("type"), source.uri)
            continue
        # Same direction as pipeline._resolve_links: the earlier item (the
        # target_uri) is `from`, the item that followed it is `to`.
        links.append(_make_link(link_type, target_document_id, document.document_id, confidence=1.0))

    thread_ts = source.metadata.get("thread_ts")
    channel = source.metadata.get("channel")
    if source.source_type == SourceType.SLACK and thread_ts and channel:
        parent_document_id = await db.find_slack_thread_parent_document(
            project_id, str(channel), str(thread_ts), source.source_id
        )
        if parent_document_id is not None and parent_document_id != document.document_id:
            links.append(
                _make_link(
                    LinkType.THREAD_PARENT,
                    document.document_id,
                    parent_document_id,
                    confidence=1.0,
                    metadata={"channel": channel, "thread_ts": thread_ts},
                )
            )

    await db.save_document_links(links)
    return links


async def link_document(db: PostgresStore, source: Source, document: Document) -> list[DocumentLink]:
    """Ingest-time entry point (PipelineStore.link_document): the Document
    row must already be saved with its version metadata applied, since
    both builders read the family / uri -> document mapping back from the
    store."""
    links: list[DocumentLink] = []
    family_key = document.metadata.version_family
    if family_key:
        links.extend(await relink_version_family(db, family_key, source.project_id))
    links.extend(await build_source_links(db, source, document))
    return links


async def backfill_document_versions_and_links(
    db: PostgresStore, project_id: str | None = None
) -> dict[str, Any]:
    """Re-derives version metadata and every link type this module owns
    for existing rows (all projects, or one). Idempotent: version keys are
    merged into metadata, links are upserted by stable id, and each
    family's PREVIOUS_VERSION chain is rebuilt whole. Returns counts for
    the CLI: documents scanned, how many got a version_family, how many
    carry an explicit marker, families with >= 2 members, and links
    written per type."""
    scanned = 0
    versioned = 0
    marked = 0
    families: set[tuple[str, str]] = set()
    pairs_to_link: list[tuple[Document, Source]] = []
    offset = 0
    while True:
        page = await db.list_documents_with_sources(project_id, limit=_BACKFILL_PAGE_SIZE, offset=offset)
        if not page:
            break
        for document, source in page:
            scanned += 1
            info = apply_version_metadata(document, source)
            if info is not None:
                versioned += 1
                if info.label is not None or info.is_final:
                    marked += 1
                families.add((info.family_key, source.project_id))
                await db.update_document_metadata(document.document_id, _version_patch(info))
            pairs_to_link.append((document, source))
        offset += len(page)

    links_by_type: Counter[str] = Counter()
    family_sizes: Counter[tuple[str, str]] = Counter()
    for family_key, family_project in sorted(families):
        members = await db.list_version_family(family_key, family_project)
        family_sizes[(family_key, family_project)] = len(members)
        for link in await relink_version_family(db, family_key, family_project):
            links_by_type[link.link_type.value] += 1
    for document, source in pairs_to_link:
        for link in await build_source_links(db, source, document):
            links_by_type[link.link_type.value] += 1

    return {
        "documents_scanned": scanned,
        "documents_versioned": versioned,
        "documents_with_marker": marked,
        "families": len(families),
        "families_multi": sum(1 for size in family_sizes.values() if size >= 2),
        "links_by_type": dict(sorted(links_by_type.items())),
    }
