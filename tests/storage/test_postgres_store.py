"""Integration tests against a real PostgreSQL instance.

Skipped unless KF_TEST_DATABASE_URL is set - the default `uv run pytest`
run needs no database. To run these locally:

    docker compose up -d postgres
    KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests/storage/ -v
"""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import ChunkRef, DocumentRef, Entity, KnowledgeObject, Relation
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.postgres_store import PostgresStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")


def _sample_records():
    src = Source(
        source_id="test-s1",
        source_type=SourceType.LOCAL_FILE,
        uri="file:///a.md",
        content_hash="h1",
        collected_at=datetime.now(timezone.utc),
    )
    doc = Document(
        document_id="test-d1",
        source=SourceRef.from_source(src),
        elements=[TextBlock(element_id="e1", text="hello")],
        metadata=DocumentMetadata(title="T"),
    )
    chunk = Chunk(
        chunk_id="test-c1",
        document_id="test-d1",
        source_id="test-s1",
        hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=0,
        text="hello",
        token_count=1,
        element_ids=["e1"],
        page_range=(1, 2),
    )
    ko = KnowledgeObject(
        knowledge_object_id="test-ko1",
        source=SourceRef.from_source(src),
        document=DocumentRef(document_id="test-d1", title="T"),
        chunk=ChunkRef(chunk_id="test-c1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0),
        summary="s",
        facts=["f"],
        entities=[Entity(name="Acme", type="ORG")],
        relations=[Relation(subject="Acme", predicate="owns", object="X")],
        topics=["t"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
    )
    return src, doc, chunk, ko


@pytest_asyncio.fixture
async def store():
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        yield db
        await db.conn.execute("DELETE FROM knowledge_objects WHERE knowledge_object_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-%'")


async def test_save_and_upsert_all_tables_is_idempotent(store):
    src, doc, chunk, ko = _sample_records()

    for _ in range(2):  # second pass exercises the ON CONFLICT upsert path
        await store.save_source(src, raw_path="/data/raw/aa/h1")
        await store.save_document(doc)
        await store.save_chunks([chunk])
        await store.save_knowledge_object(ko)

    count_row = await (await store.conn.execute(
        "SELECT count(*) FROM sources WHERE source_id = %s", (src.source_id,)
    )).fetchone()
    assert count_row[0] == 1
    count_row = await (await store.conn.execute(
        "SELECT count(*) FROM knowledge_objects WHERE knowledge_object_id = %s", (ko.knowledge_object_id,)
    )).fetchone()
    assert count_row[0] == 1

    row = await (await store.conn.execute(
        "SELECT summary, entities, topics, document_type FROM knowledge_objects WHERE knowledge_object_id = %s",
        (ko.knowledge_object_id,),
    )).fetchone()
    assert row[0] == "s"
    assert row[1] == [{"name": "Acme", "type": "ORG", "description": None}]
    assert row[2] == ["t"]
    assert row[3] == "report"


async def test_save_chunks_persists_page_range(store):
    src, doc, chunk, _ = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])

    row = await (await store.conn.execute(
        "SELECT page_start, page_end FROM chunks WHERE chunk_id = %s", (chunk.chunk_id,)
    )).fetchone()
    assert row == (1, 2)
