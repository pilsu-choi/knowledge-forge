"""document_links table + version-family queries against a real Postgres
(skipped without KF_TEST_DATABASE_URL, same as test_postgres_store.py).
Every row is tagged with PROJECT so teardown can wipe exactly this test's
data from a shared dev database."""

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from knowledge_forge.document_links import (
    apply_version_metadata,
    backfill_document_versions_and_links,
    build_source_links,
    link_document,
    relink_version_family,
)
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.link import DocumentLink, LinkType
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.storage.store import PipelineStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")
PROJECT = "test-document-links"

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")


@pytest_asyncio.fixture
async def db():
    async with PostgresStore(DSN) as store:
        await store.init_schema()
        await _cleanup(store)
        yield store
        await _cleanup(store)


async def _cleanup(store: PostgresStore) -> None:
    await store.conn.execute(
        "DELETE FROM document_links WHERE from_document_id IN (SELECT document_id FROM documents WHERE project_id = %s)"
        " OR to_document_id IN (SELECT document_id FROM documents WHERE project_id = %s)",
        (PROJECT, PROJECT),
    )
    await store.conn.execute("DELETE FROM documents WHERE project_id = %s", (PROJECT,))
    await store.conn.execute("DELETE FROM sources WHERE project_id = %s", (PROJECT,))


def _source(uri: str, source_type: SourceType = SourceType.GOOGLE_DRIVE, **metadata) -> Source:
    return Source(
        source_id=stable_id(source_type.value, uri),
        source_type=source_type,
        uri=uri,
        content_hash="h",
        collected_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
        metadata=metadata,
        project_id=PROJECT,
    )


def _document(source: Source, title: str | None = None) -> Document:
    return Document(
        document_id=stable_id("document", source.uri, "h"),
        source=SourceRef.from_source(source),
        elements=[TextBlock(element_id="e1", text="body")],
        metadata=DocumentMetadata(title=title),
    )


async def _ingest(db: PostgresStore, name: str, *, apply_version: bool = True, **metadata) -> tuple[Source, Document]:
    """Mimics pipeline.run_pipeline's store path for one Drive file."""
    source = _source(f"gdrive://{name}", name=name, **metadata)
    document = _document(source)
    if apply_version:
        apply_version_metadata(document, source)
    await db.save_source(source, raw_path=None)
    await db.save_document(document)
    return source, document


async def test_save_and_list_document_links_by_direction_and_type(db):
    _, a = await _ingest(db, "a_v1.docx")
    _, b = await _ingest(db, "a_v2.docx")
    now = datetime.now(timezone.utc)
    link = DocumentLink(
        link_id="test-dl-1",
        from_document_id=b.document_id,
        to_document_id=a.document_id,
        link_type=LinkType.PREVIOUS_VERSION,
        confidence=0.9,
        metadata={"note": "x"},
        created_at=now,
    )
    other = DocumentLink(
        link_id="test-dl-2",
        from_document_id=a.document_id,
        to_document_id=b.document_id,
        link_type=LinkType.SAME_CHANNEL_SEQUENCE,
        created_at=now,
    )
    for _ in range(2):  # upsert path
        await db.save_document_links([link, other])

    out = await db.list_document_links(b.document_id, direction="out")
    assert [lnk.link_id for lnk in out] == ["test-dl-1"]
    assert out[0].link_type is LinkType.PREVIOUS_VERSION
    assert out[0].confidence == 0.9 and out[0].metadata == {"note": "x"}

    incoming = await db.list_document_links(b.document_id, direction="in")
    assert [lnk.link_id for lnk in incoming] == ["test-dl-2"]

    both = await db.list_document_links(b.document_id)
    assert {lnk.link_id for lnk in both} == {"test-dl-1", "test-dl-2"}

    typed = await db.list_document_links(b.document_id, link_type=LinkType.PREVIOUS_VERSION.value)
    assert [lnk.link_id for lnk in typed] == ["test-dl-1"]

    with pytest.raises(ValueError):
        await db.list_document_links(b.document_id, direction="sideways")


async def test_list_version_family_orders_oldest_to_newest(db):
    names = ["보고서_최종.docx", "보고서_v2.docx", "보고서.docx", "보고서_초안.docx", "보고서_v10.docx"]
    docs = {}
    for name in names:
        _, doc = await _ingest(db, name)
        docs[doc.document_id] = name

    family = await db.list_version_family("보고서", PROJECT)
    assert [docs[d.document_id] for d in family] == [
        "보고서_초안.docx",
        "보고서.docx",
        "보고서_v2.docx",
        "보고서_v10.docx",
        "보고서_최종.docx",
    ]
    assert family[-1].metadata.is_final is True
    assert family[2].metadata.version_number == 2.0
    # a different project sees nothing
    assert await db.list_version_family("보고서", "some-other-project") == []


async def test_link_document_builds_previous_version_chain_and_relinks_out_of_order(db):
    s1, d1 = await _ingest(db, "계획서_v1.pptx")
    await link_document(db, s1, d1)
    assert await db.list_document_links(d1.document_id) == []

    s3, d3 = await _ingest(db, "계획서_v3.pptx")
    links = await link_document(db, s3, d3)
    assert [(lnk.from_document_id, lnk.to_document_id) for lnk in links] == [(d3.document_id, d1.document_id)]
    assert links[0].metadata == {"version_family": "계획서", "from_version": "v3", "to_version": "v1"}

    # v2 arrives late: v3 must now point at v2, not v1
    s2, d2 = await _ingest(db, "계획서_v2.pptx")
    await link_document(db, s2, d2)
    v3_out = await db.list_document_links(d3.document_id, direction="out", link_type="previous_version")
    assert [lnk.to_document_id for lnk in v3_out] == [d2.document_id]
    v2_out = await db.list_document_links(d2.document_id, direction="out", link_type="previous_version")
    assert [lnk.to_document_id for lnk in v2_out] == [d1.document_id]
    assert await db.list_document_links(d1.document_id, direction="out") == []

    # re-running is idempotent (stable link ids)
    await relink_version_family(db, "계획서", PROJECT)
    count = await (await db.conn.execute(
        "SELECT count(*) FROM document_links WHERE link_type = 'previous_version' AND from_document_id = ANY(%s)",
        ([d1.document_id, d2.document_id, d3.document_id],),
    )).fetchone()
    assert count[0] == 2


async def test_unmarked_duplicates_are_not_chained_but_unmarked_original_is(db):
    sa, da = await _ingest(db, "image.png")
    await link_document(db, sa, da)
    # same name, different Drive file -> a second, distinct document
    sb = _source("gdrive://image-2.png", name="image.png")
    db_ = _document(sb)
    apply_version_metadata(db_, sb)
    await db.save_source(sb, raw_path=None)
    await db.save_document(db_)
    assert await link_document(db, sb, db_) == []

    so, do = await _ingest(db, "spec.docx")
    await link_document(db, so, do)
    s2, d2 = await _ingest(db, "spec_v2.docx")
    links = await link_document(db, s2, d2)
    assert [(lnk.from_document_id, lnk.to_document_id) for lnk in links] == [(d2.document_id, do.document_id)]


async def test_build_source_links_resolves_target_uri_and_slack_thread_parent(db):
    # a timeline episode holding the thread parent message, ingested earlier
    parent = _source(
        "slack://C1/episode/100.0",
        SourceType.SLACK,
        channel="C1",
        message_ts=["100.0", "101.0"],
    )
    parent_doc = _document(parent)
    await db.save_source(parent, raw_path=None)
    await db.save_document(parent_doc)

    # the next timeline episode links back to the previous one, and a thread
    # episode whose parent message lives in that first episode
    nxt = _source(
        "slack://C1/episode/5000.0",
        SourceType.SLACK,
        channel="C1",
        message_ts=["5000.0"],
        links=[{"type": "same_channel_sequence", "target_uri": "slack://C1/episode/100.0"}],
    )
    nxt_doc = _document(nxt)
    await db.save_source(nxt, raw_path=None)
    await db.save_document(nxt_doc)
    links = await build_source_links(db, nxt, nxt_doc)
    assert [(lnk.link_type, lnk.from_document_id, lnk.to_document_id) for lnk in links] == [
        (LinkType.SAME_CHANNEL_SEQUENCE, parent_doc.document_id, nxt_doc.document_id)
    ]

    thread = _source(
        "slack://C1/thread/100.0",
        SourceType.SLACK,
        channel="C1",
        thread_ts="100.0",
        message_ts=["100.0", "200.0"],
    )
    thread_doc = _document(thread)
    await db.save_source(thread, raw_path=None)
    await db.save_document(thread_doc)
    links = await build_source_links(db, thread, thread_doc)
    assert [(lnk.link_type, lnk.from_document_id, lnk.to_document_id) for lnk in links] == [
        (LinkType.THREAD_PARENT, thread_doc.document_id, parent_doc.document_id)
    ]
    assert links[0].metadata == {"channel": "C1", "thread_ts": "100.0"}

    # an unresolvable target_uri (never parsed) is skipped, not an error
    dangling = _source(
        "slack://C1/episode/9000.0",
        SourceType.SLACK,
        channel="C1",
        message_ts=["9000.0"],
        links=[{"type": "same_channel_sequence", "target_uri": "slack://C1/episode/nope"}],
    )
    dangling_doc = _document(dangling)
    await db.save_source(dangling, raw_path=None)
    await db.save_document(dangling_doc)
    assert await build_source_links(db, dangling, dangling_doc) == []

    # Slack episodes carry no version family
    assert thread_doc.metadata.version_family is None


async def test_backfill_fills_version_metadata_and_rebuilds_links(db):
    # rows written "before" this stage: no version keys, but an unrelated
    # metadata key that must survive the merge
    old_rows = []
    for name in ["설계서_v1.docx", "설계서_v2.docx", "설계서_최종.docx", "unrelated.md"]:
        source, document = await _ingest(db, name, apply_version=False)
        await db.conn.execute(
            "UPDATE documents SET metadata = metadata || '{\"title\": \"kept\"}' WHERE document_id = %s",
            (document.document_id,),
        )
        old_rows.append((source, document))
    # stagger created_at so the tie-break is deterministic
    for i, (_, document) in enumerate(old_rows):
        await db.conn.execute(
            "UPDATE documents SET created_at = %s WHERE document_id = %s",
            (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i), document.document_id),
        )
    # a gmail thread chained to the previous one via metadata.links
    first_mail = _source("gmail://thread/t1", SourceType.GMAIL, subject="주간보고 v1")
    first_doc = _document(first_mail)
    second_mail = _source(
        "gmail://thread/t2",
        SourceType.GMAIL,
        subject="Re: 주간보고 v1",
        links=[{"type": "thread_sequence", "target_uri": "gmail://thread/t1"}],
    )
    second_doc = _document(second_mail)
    for source, document in ((first_mail, first_doc), (second_mail, second_doc)):
        await db.save_source(source, raw_path=None)
        await db.save_document(document)

    stats = await backfill_document_versions_and_links(db, PROJECT)
    assert stats["documents_scanned"] == 6
    assert stats["documents_versioned"] == 6
    assert stats["documents_with_marker"] == 5  # unrelated.md has none
    assert stats["families_multi"] == 2  # 설계서 x3, 주간보고 x2
    assert stats["links_by_type"] == {"previous_version": 2, "thread_sequence": 1}

    stored = await db.get_document(old_rows[2][1].document_id)
    assert stored.metadata.title == "kept"
    assert stored.metadata.version_family == "설계서"
    assert stored.metadata.is_final is True
    assert stored.metadata.version_label == "최종"

    family = await db.list_version_family("설계서", PROJECT)
    assert [d.metadata.version_label for d in family] == ["v1", "v2", "최종"]
    final_out = await db.list_document_links(family[-1].document_id, direction="out", link_type="previous_version")
    assert [lnk.to_document_id for lnk in final_out] == [family[1].document_id]

    # both mails share a family ("Re:" stripped) but carry the same marker
    # (v1 twice) - copies of one version, so no previous_version edge between
    # them; the thread_sequence edge comes from metadata.links
    seq = await db.list_document_links(second_doc.document_id, direction="in", link_type="thread_sequence")
    assert [lnk.from_document_id for lnk in seq] == [first_doc.document_id]

    # second run: same counts, no duplicate rows
    again = await backfill_document_versions_and_links(db, PROJECT)
    assert again["links_by_type"] == stats["links_by_type"]
    total = await (await db.conn.execute(
        "SELECT count(*) FROM document_links WHERE from_document_id IN "
        "(SELECT document_id FROM documents WHERE project_id = %s)",
        (PROJECT,),
    )).fetchone()
    assert total[0] == 3


async def test_pipeline_store_facade_delegates(db, tmp_path):
    async with PipelineStore(DSN, tmp_path) as store:
        s1, d1 = await _ingest(db, "facade_v1.md")
        await store.link_document(s1, d1)
        s2, d2 = await _ingest(db, "facade_v2.md")
        links = await store.link_document(s2, d2)
        assert len(links) == 1
        assert [d.document_id for d in await store.list_version_family("facade", PROJECT)] == [
            d1.document_id,
            d2.document_id,
        ]
        assert [lnk.to_document_id for lnk in await store.list_document_links(d2.document_id, direction="out")] == [
            d1.document_id
        ]
        stats = await store.backfill_document_versions_and_links(PROJECT)
        assert stats["links_by_type"] == {"previous_version": 1}
