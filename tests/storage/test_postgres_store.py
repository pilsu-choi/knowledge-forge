"""Integration tests against a real PostgreSQL instance.

Skipped unless KF_TEST_DATABASE_URL is set - the default `uv run pytest`
run needs no database. To run these locally:

    docker compose up -d postgres
    KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests/storage/ -v
"""

import os
from datetime import datetime, timezone

import psycopg
import pytest
import pytest_asyncio
from pgvector import Vector

from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import (
    DocumentRef,
    Evidence,
    KnowledgeObject,
    KnowledgeObjectType,
)
from knowledge_forge.models.relation import Relation, RelationType
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
    entity_ko = KnowledgeObject(
        knowledge_object_id="test-ko1",
        type=KnowledgeObjectType.ENTITY,
        source=SourceRef.from_source(src),
        document=DocumentRef(document_id="test-d1", title="T"),
        evidence=[Evidence(chunk_id="test-c1", document_id="test-d1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="Acme",
        content={"name": "Acme", "entity_type": "ORG", "description": None},
        topics=["t"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="h1",
    )
    fact_ko = KnowledgeObject(
        knowledge_object_id="test-ko2",
        type=KnowledgeObjectType.FACT,
        source=SourceRef.from_source(src),
        document=DocumentRef(document_id="test-d1", title="T"),
        evidence=[Evidence(chunk_id="test-c1", document_id="test-d1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="Acme owns X",
        content={"statement": "Acme owns X"},
        topics=["t"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="h1",
    )
    relation = Relation(
        relation_id="test-rel1",
        type=RelationType.MENTIONS,
        subject_id="test-ko2",
        object_id="test-ko1",
        label="owns",
        confidence=0.8,
        evidence=["test-c1"],
        created_at=datetime.now(timezone.utc),
    )
    return src, doc, chunk, [entity_ko, fact_ko], relation


@pytest_asyncio.fixture
async def store():
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        yield db
        await db.conn.execute("DELETE FROM relations WHERE relation_id LIKE 'test-%'")
        await db.conn.execute(
            "DELETE FROM knowledge_object_embeddings WHERE knowledge_object_id LIKE 'test-%' "
            "OR chunk_id LIKE 'test-%' OR embedding_id LIKE 'test-%'"
        )
        await db.conn.execute("DELETE FROM knowledge_objects WHERE knowledge_object_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM sync_cursors WHERE source LIKE 'test-%'")


async def test_save_and_upsert_all_tables_is_idempotent(store):
    src, doc, chunk, objects, relation = _sample_records()

    for _ in range(2):  # second pass exercises the ON CONFLICT upsert path
        await store.save_source(src, raw_path="/data/raw/aa/h1")
        await store.save_document(doc)
        await store.save_chunks([chunk])
        await store.save_knowledge_objects(objects)
        await store.save_relations([relation])

    count_row = await (await store.conn.execute(
        "SELECT count(*) FROM sources WHERE source_id = %s", (src.source_id,)
    )).fetchone()
    assert count_row[0] == 1
    count_row = await (await store.conn.execute(
        "SELECT count(*) FROM knowledge_objects WHERE knowledge_object_id = %s", (objects[0].knowledge_object_id,)
    )).fetchone()
    assert count_row[0] == 1
    count_row = await (await store.conn.execute(
        "SELECT count(*) FROM relations WHERE relation_id = %s", (relation.relation_id,)
    )).fetchone()
    assert count_row[0] == 1

    row = await (await store.conn.execute(
        "SELECT type, summary, content, topics, document_type FROM knowledge_objects WHERE knowledge_object_id = %s",
        (objects[0].knowledge_object_id,),
    )).fetchone()
    assert row[0] == "entity"
    assert row[1] == "Acme"
    assert row[2] == {"name": "Acme", "entity_type": "ORG", "description": None}
    assert row[3] == ["t"]
    assert row[4] == "report"

    rel_row = await (await store.conn.execute(
        "SELECT type, subject_id, object_id, label FROM relations WHERE relation_id = %s",
        (relation.relation_id,),
    )).fetchone()
    assert rel_row == ("mentions", "test-ko2", "test-ko1", "owns")


async def test_list_knowledge_objects_by_source_reconstructs_full_records(store):
    src, doc, chunk, objects, relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)
    await store.save_relations([relation])

    fetched = await store.list_knowledge_objects_by_source(src.source_id)

    assert {ko.knowledge_object_id for ko in fetched} == {"test-ko1", "test-ko2"}
    entity = next(ko for ko in fetched if ko.knowledge_object_id == "test-ko1")
    assert entity.source.source_id == src.source_id
    assert entity.source.uri == src.uri
    assert entity.document.document_id == "test-d1"
    assert entity.evidence[0].chunk_id == "test-c1"
    assert entity.summary == "Acme"
    assert entity.content == {"name": "Acme", "entity_type": "ORG", "description": None}


async def test_list_knowledge_objects_by_source_returns_empty_for_unknown_source(store):
    assert await store.list_knowledge_objects_by_source("no-such-source") == []


async def test_list_knowledge_objects_by_source_excludes_superseded_objects(store):
    """extraction/versioning.py's O(new x prior) supersedes matching only
    needs the current "head" of each claim's version chain, not every
    KnowledgeObject a source has ever produced across every revision - see
    PostgresStore.list_knowledge_objects_by_source's docstring. test-ko1
    (superseded by a newer object below) must not come back; test-ko2
    (never superseded) and the new object must."""
    src, doc, chunk, objects, relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)
    await store.save_relations([relation])

    newer = KnowledgeObject(
        knowledge_object_id="test-ko1-v2",
        type=KnowledgeObjectType.ENTITY,
        source=SourceRef.from_source(src),
        document=DocumentRef(document_id="test-d1", title="T"),
        evidence=[Evidence(chunk_id="test-c1", document_id="test-d1", hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="Acme Corp",
        content={"name": "Acme Corp", "entity_type": "ORG", "description": None},
        topics=["t"],
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="h1",
        supersedes="test-ko1",
        version=2,
    )
    await store.save_knowledge_objects([newer])

    fetched = await store.list_knowledge_objects_by_source(src.source_id)

    assert {ko.knowledge_object_id for ko in fetched} == {"test-ko2", "test-ko1-v2"}


async def test_save_chunks_persists_page_range(store):
    src, doc, chunk, _, _ = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])

    row = await (await store.conn.execute(
        "SELECT page_start, page_end FROM chunks WHERE chunk_id = %s", (chunk.chunk_id,)
    )).fetchone()
    assert row == (1, 2)


async def test_save_source_defaults_project_id(store):
    src, doc, _, _, _ = _sample_records()
    await store.save_source(src, raw_path=None)

    fetched = await store.get_source(src.source_id)
    assert fetched.project_id == "default"


async def test_list_sources_filters_by_project_id(store):
    src, doc, _, _, _ = _sample_records()
    src2 = src.model_copy(update={"source_id": "test-s2", "project_id": "proj-a"})
    await store.save_source(src, raw_path=None)
    await store.save_source(src2, raw_path=None)

    proj_a = await store.list_sources(project_id="proj-a", limit=200)
    assert {s.source_id for s in proj_a} == {"test-s2"}

    default_project = await store.list_sources(project_id="default", limit=200)
    assert src.source_id in {s.source_id for s in default_project}
    assert "test-s2" not in {s.source_id for s in default_project}


async def test_get_source_returns_none_for_unknown_id(store):
    assert await store.get_source("no-such-source") is None


async def test_save_document_inherits_project_id_from_source(store):
    src, doc, _, _, _ = _sample_records()
    src = src.model_copy(update={"project_id": "proj-a"})
    doc = doc.model_copy(update={"source": SourceRef.from_source(src)})
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)

    fetched = await store.get_document(doc.document_id)
    assert fetched.source.project_id == "proj-a"

    listed = await store.list_documents(project_id="proj-a", limit=200)
    assert {d.document_id for d in listed} == {doc.document_id}

    listed_other = await store.list_documents(project_id="default", limit=200)
    assert doc.document_id not in {d.document_id for d in listed_other}


async def test_list_documents_filters_by_source_id(store):
    src, doc, _, _, _ = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)

    listed = await store.list_documents(source_id=src.source_id, limit=200)
    assert {d.document_id for d in listed} == {doc.document_id}

    listed_other = await store.list_documents(source_id="no-such-source", limit=200)
    assert listed_other == []


async def test_get_document_returns_none_for_unknown_id(store):
    assert await store.get_document("no-such-document") is None


async def test_sync_cursor_round_trips_and_upserts(store):
    assert await store.get_sync_cursor("test-source") is None

    await store.save_sync_cursor("test-source", {"channels": {"c1": "1.0"}}, datetime.now(timezone.utc))
    assert await store.get_sync_cursor("test-source") == {"channels": {"c1": "1.0"}}

    # A second save for the same source overwrites rather than duplicating.
    await store.save_sync_cursor("test-source", {"channels": {"c1": "2.0"}}, datetime.now(timezone.utc))
    assert await store.get_sync_cursor("test-source") == {"channels": {"c1": "2.0"}}

    row = await (await store.conn.execute(
        "SELECT count(*) FROM sync_cursors WHERE source = %s", ("test-source",)
    )).fetchone()
    assert row[0] == 1


async def test_save_embeddings_upsert_is_idempotent(store):
    from knowledge_forge.embedding.base import EmbeddingRecord

    src, doc, chunk, objects, _relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    ko_id = objects[0].knowledge_object_id
    record = EmbeddingRecord(
     target_kind="knowledge_object",
     knowledge_object_id=ko_id,
     document_id="test-d1",
        embedding_model="test-model",
        embedding=[0.1] * 1536,
        embedded_text="Acme",
        embedded_text_hash="hash1",
        project_id=src.project_id,
    )
    await store.save_embeddings([record])

    updated = EmbeddingRecord(
     target_kind="knowledge_object",
     knowledge_object_id=ko_id,
     document_id="test-d1",
        embedding_model="test-model",
        embedding=[0.2] * 1536,
        embedded_text="Acme v2",
        embedded_text_hash="hash2",
        project_id=src.project_id,
    )
    await store.save_embeddings([updated])

    count_row = await (await store.conn.execute(
        "SELECT count(*) FROM knowledge_object_embeddings WHERE knowledge_object_id = %s AND embedding_model = %s",
        (ko_id, "test-model"),
    )).fetchone()
    assert count_row[0] == 1

    row = await (await store.conn.execute(
        "SELECT embedded_text, embedded_text_hash FROM knowledge_object_embeddings "
        "WHERE knowledge_object_id = %s AND embedding_model = %s",
        (ko_id, "test-model"),
    )).fetchone()
    assert row[0] == "Acme v2"
    assert row[1] == "hash2"


async def test_list_knowledge_objects_missing_embedding(store):
    from knowledge_forge.embedding.base import EmbeddingRecord

    src, doc, chunk, objects, _relation = _sample_records()
    # A populated dev database has thousands of unembedded KOs ahead of this
    # test's rows in extracted_at order - scope the "missing" scans to a
    # project only this test writes so `limit` can't hide them.
    src = src.model_copy(update={"project_id": "test-proj-missing-ko"})
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    entity_id = objects[0].knowledge_object_id
    fact_id = objects[1].knowledge_object_id

    # Neither KO has an embedding yet - both should show up as missing.
    missing = await store.list_knowledge_objects_missing_embedding("test-model", src.project_id, 50)
    missing_ids = {ko.knowledge_object_id for ko in missing}
    assert entity_id in missing_ids
    assert fact_id in missing_ids

    # Embed one of them - it should drop out of the missing list.
    await store.save_embeddings(
        [
            EmbeddingRecord(
                target_kind="knowledge_object",
                knowledge_object_id=entity_id,
                document_id="test-d1",
                embedding_model="test-model",
                embedding=[0.1] * 1536,
                embedded_text="Acme",
                embedded_text_hash="hash1",
                project_id=src.project_id,
            )
        ]
    )

    missing = await store.list_knowledge_objects_missing_embedding("test-model", src.project_id, 50)
    missing_ids = {ko.knowledge_object_id for ko in missing}
    assert entity_id not in missing_ids
    assert fact_id in missing_ids

    # A different embedding_model doesn't count as "embedded" for this model.
    missing_other_model = await store.list_knowledge_objects_missing_embedding("other-model", src.project_id, 50)
    missing_other_ids = {ko.knowledge_object_id for ko in missing_other_model}
    assert entity_id in missing_other_ids
    assert fact_id in missing_other_ids

    # project_id filter excludes KOs from other projects.
    missing_wrong_project = await store.list_knowledge_objects_missing_embedding(
        "test-model", "no-such-project", 50
    )
    assert fact_id not in {ko.knowledge_object_id for ko in missing_wrong_project}

    missing_right_project = await store.list_knowledge_objects_missing_embedding(
        "test-model", src.project_id, 50
    )
    assert fact_id in {ko.knowledge_object_id for ko in missing_right_project}


async def test_search_by_embedding_orders_by_similarity_and_filters(store):
    from knowledge_forge.embedding.base import EmbeddingRecord

    src, doc, chunk, objects, _relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    entity_id = objects[0].knowledge_object_id  # type: entity
    fact_id = objects[1].knowledge_object_id  # type: fact

    # Two orthogonal unit vectors - a query equal to vector_a has cosine
    # distance 0 (score 1.0) to it and distance 1 (score 0.0) to vector_b, so
    # ordering/scores are exact rather than approximate-by-luck.
    vector_a = [1.0] + [0.0] * 1535
    vector_b = [0.0, 1.0] + [0.0] * 1534

    await store.save_embeddings(
        [
            EmbeddingRecord(
                target_kind="knowledge_object",
                knowledge_object_id=entity_id,
                document_id="test-d1",
                embedding_model="test-model",
                embedding=vector_a,
                embedded_text="Acme",
                embedded_text_hash="hash1",
                project_id=src.project_id,
            ),
            EmbeddingRecord(
                target_kind="knowledge_object",
                knowledge_object_id=fact_id,
                document_id="test-d1",
                embedding_model="test-model",
                embedding=vector_b,
                embedded_text="Acme owns X",
                embedded_text_hash="hash2",
                project_id=src.project_id,
            ),
        ]
    )

    results = await store.search_by_embedding(vector_a, "test-model", None, None, 10)
    assert [r["knowledge_object_id"] for r in results] == [entity_id, fact_id]
    assert results[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert results[1]["score"] == pytest.approx(0.0, abs=1e-6)

    fact_only = await store.search_by_embedding(vector_a, "test-model", None, "fact", 10)
    assert [r["knowledge_object_id"] for r in fact_only] == [fact_id]

    wrong_project = await store.search_by_embedding(
        vector_a, "test-model", "no-such-project", None, 10
    )
    assert wrong_project == []

    other_model = await store.search_by_embedding(vector_a, "other-model", None, None, 10)
    assert other_model == []


async def test_search_by_embedding_filters_by_the_sources_content_timestamp(store):
    """Filtering must follow the *source's* real-world timestamp
    (modified_at, falling back to collected_at), not `extracted_at` - a
    batch extraction run stamps every KnowledgeObject it touches with the
    same `extracted_at` (whenever the pipeline happened to run) regardless
    of how old the underlying message actually is, so both KOs here share a
    late `extracted_at` but come from sources with very different
    `modified_at` - only the latter should affect the filter."""
    from knowledge_forge.embedding.base import EmbeddingRecord

    same_run_time = datetime(2026, 9, 2, tzinfo=timezone.utc)  # both KOs "extracted" on the same batch run

    old_src = Source(
        source_id="test-s-old",
        source_type=SourceType.SLACK,
        uri="slack://C1/old",
        content_hash="h-old",
        collected_at=same_run_time,
        modified_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # message actually sent long ago
    )
    new_src = Source(
        source_id="test-s-new",
        source_type=SourceType.SLACK,
        uri="slack://C1/new",
        content_hash="h-new",
        collected_at=same_run_time,
        modified_at=datetime(2026, 9, 1, tzinfo=timezone.utc),  # message actually sent recently
    )
    for src in (old_src, new_src):
        await store.save_source(src, raw_path=None)
        doc = Document(
            document_id=f"test-d-{src.source_id}",
            source=SourceRef.from_source(src),
            elements=[TextBlock(element_id="e1", text="hello")],
            metadata=DocumentMetadata(title="T"),
        )
        await store.save_document(doc)
        chunk = Chunk(
            chunk_id=f"test-c-{src.source_id}",
            document_id=doc.document_id,
            source_id=src.source_id,
            hierarchy_level=HierarchyLevel.PARAGRAPH,
            sequence_index=0,
            text="hello",
            token_count=1,
            element_ids=["e1"],
            page_range=(1, 2),
        )
        await store.save_chunks([chunk])

    def _ko(ko_id: str, src: Source) -> KnowledgeObject:
        return KnowledgeObject(
            knowledge_object_id=ko_id,
            type=KnowledgeObjectType.FACT,
            source=SourceRef.from_source(src),
            document=DocumentRef(document_id=f"test-d-{src.source_id}", title="T"),
            evidence=[
                Evidence(
                    chunk_id=f"test-c-{src.source_id}",
                    document_id=f"test-d-{src.source_id}",
                    hierarchy_level=HierarchyLevel.PARAGRAPH,
                    sequence_index=0,
                )
            ],
            summary="s",
            content={"statement": "s"},
            topics=["t"],
            document_type="report",
            extracted_at=same_run_time,
            model_used="claude-opus-5",
            pipeline_version="0.1.0",
            source_hash=src.content_hash,
        )

    old_ko = _ko("test-ko-old", old_src)
    new_ko = _ko("test-ko-new", new_src)
    await store.save_knowledge_objects([old_ko, new_ko])

    vector = [1.0] + [0.0] * 1535
    await store.save_embeddings(
        [
            EmbeddingRecord(
                target_kind="knowledge_object",
                knowledge_object_id=old_ko.knowledge_object_id,
                document_id=old_ko.document.document_id,
                embedding_model="test-model",
                embedding=vector,
                embedded_text="old",
                embedded_text_hash="hash-old",
                project_id=old_src.project_id,
            ),
            EmbeddingRecord(
                target_kind="knowledge_object",
                knowledge_object_id=new_ko.knowledge_object_id,
                document_id=new_ko.document.document_id,
                embedding_model="test-model",
                embedding=vector,
                embedded_text="new",
                embedded_text_hash="hash-new",
                project_id=new_src.project_id,
            ),
        ]
    )

    since_only = await store.search_by_embedding(
        vector, "test-model", None, None, 10, since=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert {r["knowledge_object_id"] for r in since_only} == {new_ko.knowledge_object_id}
    assert since_only[0]["content_at"] == "2026-09-01T00:00:00+00:00"

    until_only = await store.search_by_embedding(
        vector, "test-model", None, None, 10, until=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert {r["knowledge_object_id"] for r in until_only} == {old_ko.knowledge_object_id}

    both_bounds = await store.search_by_embedding(
        vector,
        "test-model",
        None,
        None,
        10,
        since=datetime(2019, 1, 1, tzinfo=timezone.utc),
        until=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert {r["knowledge_object_id"] for r in both_bounds} == {old_ko.knowledge_object_id, new_ko.knowledge_object_id}


async def test_execute_readonly_query_returns_rows_and_columns(store):
    src, doc, chunk, objects, _relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    rows, columns = await store.execute_readonly_query(
        "SELECT knowledge_object_id, type FROM knowledge_objects WHERE knowledge_object_id LIKE 'test-%' ORDER BY knowledge_object_id",
        limit=50,
    )

    assert columns == ["knowledge_object_id", "type"]
    assert rows == [(objects[0].knowledge_object_id, "entity"), (objects[1].knowledge_object_id, "fact")]


async def test_execute_readonly_query_enforces_the_limit_regardless_of_the_querys_own_limit(store):
    src, doc, chunk, objects, _relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    rows, _columns = await store.execute_readonly_query(
        "SELECT knowledge_object_id FROM knowledge_objects WHERE knowledge_object_id LIKE 'test-%' LIMIT 1000",
        limit=1,
    )

    assert len(rows) == 1


async def test_execute_readonly_query_rejects_a_write_at_the_database_level(store):
    """search/sql.py's `_validate_readonly_sql` would already reject this by
    string alone (it contains "UPDATE"), but this test calls the store
    method directly - bypassing that check on purpose - with a write
    disguised as a data-modifying CTE (`WITH x AS (UPDATE ... RETURNING ...)
    SELECT ...`, syntactically a query expression so it survives being
    wrapped as a subquery) to prove the second independent defense layer
    (`SET TRANSACTION READ ONLY`) rejects it entirely on its own."""
    src, doc, chunk, objects, _relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    disguised_write = (
        "WITH x AS (UPDATE knowledge_objects SET summary = 'hacked' "
        f"WHERE knowledge_object_id = '{objects[0].knowledge_object_id}' RETURNING knowledge_object_id) "
        "SELECT * FROM x"
    )
    with pytest.raises(psycopg.Error):
        await store.execute_readonly_query(disguised_write, limit=10)

    unchanged = await store.get_knowledge_object(objects[0].knowledge_object_id)
    assert unchanged.summary != "hacked"


async def test_connect_succeeds_and_embeddings_work_on_a_database_with_no_vector_extension_yet():
    """A brand new database has no `vector` type at all until `init_schema()`
    creates the extension - `connect()` must not raise before `init_schema()`
    is even reachable (real bug found by running this project's own onboarding
    steps - `docker compose up -d postgres` + `knowledge-forge db init` -
    against a genuinely fresh database: `connect()` used to call
    `register_vector_async()` unconditionally, which raised
    `vector type not found in the database` and made `db init` impossible to
    ever complete on a first run)."""
    import urllib.parse

    import psycopg

    parts = urllib.parse.urlsplit(DSN)
    admin_dsn = urllib.parse.urlunsplit(parts._replace(path="/postgres"))
    fresh_db = "kf_bootstrap_test"
    fresh_dsn = urllib.parse.urlunsplit(parts._replace(path=f"/{fresh_db}"))

    admin_conn = await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True)
    try:
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {fresh_db}")
        await admin_conn.execute(f"CREATE DATABASE {fresh_db}")

        async with PostgresStore(fresh_dsn) as store:  # must not raise
            await store.init_schema()

            from knowledge_forge.embedding.base import EmbeddingRecord

            src, doc, chunk, objects, _relation = _sample_records()
            await store.save_source(src, raw_path=None)
            await store.save_document(doc)
            await store.save_chunks([chunk])
            await store.save_knowledge_objects(objects)

            ko_id = objects[0].knowledge_object_id
            vector = [1.0] + [0.0] * 1535
            await store.save_embeddings(
                [
                    EmbeddingRecord(
                        target_kind="knowledge_object",
                        knowledge_object_id=ko_id,
                        document_id="test-d1",
                        embedding_model="test-model",
                        embedding=vector,
                        embedded_text="Acme",
                        embedded_text_hash="hash1",
                        project_id=src.project_id,
                    )
                ]
            )
            results = await store.search_by_embedding(vector, "test-model", None, None, 10)
            assert ko_id in {r["knowledge_object_id"] for r in results}
    finally:
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {fresh_db}")
        await admin_conn.close()


async def _save_sample_graph(store):
    src, doc, chunk, objects, _relation = _sample_records()
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)
    return src, doc, chunk, objects


def _chunk_record(chunk, project_id, vector, text="hello", model="test-model"):
    from knowledge_forge.embedding.base import EmbeddingRecord

    return EmbeddingRecord(
        target_kind="chunk",
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        embedding_model=model,
        embedding=vector,
        embedded_text=text,
        embedded_text_hash=f"hash-{text}",
        project_id=project_id,
    )


async def test_chunk_embedding_upsert_is_idempotent_and_keeps_grain_columns(store):
    src, _doc, chunk, _objects = await _save_sample_graph(store)

    await store.save_embeddings([_chunk_record(chunk, src.project_id, [0.1] * 1536, text="v1")])
    await store.save_embeddings([_chunk_record(chunk, src.project_id, [0.2] * 1536, text="v2")])

    row = await (await store.conn.execute(
        "SELECT count(*), min(target_kind), min(knowledge_object_id), min(document_id), min(embedded_text) "
        "FROM knowledge_object_embeddings WHERE chunk_id = %s AND embedding_model = %s",
        (chunk.chunk_id, "test-model"),
    )).fetchone()
    assert row == (1, "chunk", None, chunk.document_id, "v2")


async def test_knowledge_object_embedding_rows_carry_document_id(store):
    """document_id is the identifier both grains share - a KO-grain row
    must carry it too, or the evaluation can't score KO-mode retrieval at
    document level."""
    from knowledge_forge.embedding.base import EmbeddingRecord

    src, doc, _chunk, objects = await _save_sample_graph(store)
    await store.save_embeddings(
        [
            EmbeddingRecord(
                target_kind="knowledge_object",
                knowledge_object_id=objects[0].knowledge_object_id,
                document_id=doc.document_id,
                embedding_model="test-model",
                embedding=[0.1] * 1536,
                embedded_text="Acme",
                embedded_text_hash="h",
                project_id=src.project_id,
            )
        ]
    )
    row = await (await store.conn.execute(
        "SELECT target_kind, chunk_id, document_id FROM knowledge_object_embeddings WHERE knowledge_object_id = %s",
        (objects[0].knowledge_object_id,),
    )).fetchone()
    assert row == ("knowledge_object", None, doc.document_id)


async def test_schema_rejects_a_row_whose_ids_do_not_match_its_target_kind(store):
    """The CHECK constraint is the last line of defence behind
    EmbeddingRecord.__post_init__ - raw SQL can't sneak in a row that
    claims one grain but points at the other."""
    _src, doc, chunk, _objects = await _save_sample_graph(store)
    with pytest.raises(psycopg.errors.CheckViolation):
        await store.conn.execute(
            """
            INSERT INTO knowledge_object_embeddings (embedding_id, target_kind, knowledge_object_id, chunk_id,
                document_id, embedding_model, embedding, embedded_text, embedded_text_hash, project_id, created_at)
            VALUES ('test-bad', 'knowledge_object', NULL, %s, %s, 'm', %s, 't', 'h', 'default', now())
            """,
            (chunk.chunk_id, doc.document_id, Vector([0.0] * 1536)),
        )


async def test_list_chunks_missing_embedding_filters_by_model_and_project(store):
    src, doc, chunk, objects, _relation = _sample_records()
    src = src.model_copy(update={"project_id": "test-proj-missing-chunk"})  # see the KO variant above
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    missing = await store.list_chunks_missing_embedding("test-model", src.project_id, 50)
    assert (chunk.chunk_id, src.project_id) in {(c.chunk_id, pid) for c, pid in missing}
    found = next(c for c, _ in missing if c.chunk_id == chunk.chunk_id)
    assert found.text == chunk.text and found.page_range == chunk.page_range

    await store.save_embeddings([_chunk_record(chunk, src.project_id, [0.1] * 1536)])

    assert chunk.chunk_id not in {c.chunk_id for c, _ in await store.list_chunks_missing_embedding("test-model", src.project_id, 50)}
    # A different model doesn't count as embedded for this model.
    assert chunk.chunk_id in {c.chunk_id for c, _ in await store.list_chunks_missing_embedding("other-model", src.project_id, 50)}
    # And a KO-grain embedding of the same document doesn't count as the chunk being embedded either.
    assert chunk.chunk_id in {c.chunk_id for c, _ in await store.list_chunks_missing_embedding("other-model", src.project_id, 50)}
    assert await store.list_chunks_missing_embedding("other-model", "no-such-project", 50) == []


async def test_search_by_embedding_mixes_grains_by_default_and_narrows_on_target_kind(store):
    from knowledge_forge.embedding.base import EmbeddingRecord

    src, doc, chunk, objects = await _save_sample_graph(store)
    ko_id = objects[0].knowledge_object_id
    vector_a = [1.0] + [0.0] * 1535
    vector_b = [0.0, 1.0] + [0.0] * 1534
    long_text = "x" * 2000

    await store.save_embeddings(
        [
            EmbeddingRecord(
                target_kind="knowledge_object",
                knowledge_object_id=ko_id,
                document_id=doc.document_id,
                embedding_model="test-model",
                embedding=vector_b,
                embedded_text="Acme",
                embedded_text_hash="h",
                project_id=src.project_id,
            ),
            _chunk_record(chunk, src.project_id, vector_a, text=long_text),
        ]
    )

    both = await store.search_by_embedding(vector_a, "test-model", None, None, 10)
    assert [(r["target_kind"], r["score"]) for r in both] == [("chunk", pytest.approx(1.0)), ("knowledge_object", pytest.approx(0.0))]
    chunk_hit, ko_hit = both
    # Both grains expose the shared document id and their own id only.
    assert chunk_hit["document_id"] == ko_hit["document_id"] == doc.document_id
    assert (chunk_hit["chunk_id"], chunk_hit["knowledge_object_id"]) == (chunk.chunk_id, None)
    assert (ko_hit["chunk_id"], ko_hit["knowledge_object_id"]) == (None, ko_id)
    # Chunk hit renders as type 'chunk' with a capped text summary; KO hit keeps its KO fields.
    assert chunk_hit["type"] == "chunk" and len(chunk_hit["summary"]) == 500 and chunk_hit["topics"] == []
    assert chunk_hit["hierarchy_level"] == "paragraph" and chunk_hit["sequence_index"] == 0
    assert ko_hit["type"] == "entity" and ko_hit["summary"] == "Acme" and ko_hit["topics"] == ["t"]

    only_chunks = await store.search_by_embedding(vector_a, "test-model", None, None, 10, target_kind="chunk")
    assert [r["chunk_id"] for r in only_chunks] == [chunk.chunk_id]
    only_kos = await store.search_by_embedding(vector_a, "test-model", None, None, 10, target_kind="knowledge_object")
    assert [r["knowledge_object_id"] for r in only_kos] == [ko_id]
    # A KO `type` filter can only ever match KO rows, so it implicitly drops chunk hits.
    typed = await store.search_by_embedding(vector_a, "test-model", None, "entity", 10)
    assert [r["knowledge_object_id"] for r in typed] == [ko_id]
    # `document_ids` scopes the search to those documents (grain expansion in search/retrieve.py).
    in_doc = await store.search_by_embedding(vector_a, "test-model", None, None, 10, document_ids=[doc.document_id])
    assert len(in_doc) == 2
    assert await store.search_by_embedding(vector_a, "test-model", None, None, 10, document_ids=["missing"]) == []


async def test_init_schema_migrates_the_pre_chunk_embeddings_table_in_place():
    """A database created before target_kind existed has the old
    (knowledge_object_id, embedding_model)-keyed table; init_schema must
    replace it (embeddings are derivable) rather than fail on the PK
    mismatch or leave the old shape behind."""
    import urllib.parse

    parts = urllib.parse.urlsplit(DSN)
    admin_dsn = urllib.parse.urlunsplit(parts._replace(path="/postgres"))
    fresh_db = "kf_embeddings_migration_test"
    fresh_dsn = urllib.parse.urlunsplit(parts._replace(path=f"/{fresh_db}"))

    admin_conn = await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True)
    try:
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {fresh_db}")
        await admin_conn.execute(f"CREATE DATABASE {fresh_db}")
        async with PostgresStore(fresh_dsn) as store:
            await store.init_schema()
            await store.conn.execute("DROP TABLE knowledge_object_embeddings")
            await store.conn.execute(
                """
                CREATE TABLE knowledge_object_embeddings (
                    knowledge_object_id TEXT NOT NULL REFERENCES knowledge_objects(knowledge_object_id),
                    embedding_model TEXT NOT NULL, embedding vector(1536) NOT NULL, embedded_text TEXT NOT NULL,
                    embedded_text_hash TEXT NOT NULL, project_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (knowledge_object_id, embedding_model))
                """
            )
            await store.init_schema()  # must migrate, and be idempotent on the next call
            await store.init_schema()
            cols = await (await store.conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'knowledge_object_embeddings' "
                "ORDER BY ordinal_position"
            )).fetchall()
            assert [c[0] for c in cols][:5] == ["embedding_id", "target_kind", "knowledge_object_id", "chunk_id", "document_id"]
    finally:
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {fresh_db}")
        await admin_conn.close()


async def test_list_chunks_by_document_orders_by_sequence_index_and_optionally_filters_level(store):
    src, doc, _, _, _ = _sample_records()
    await store.save_source(src, None)
    await store.save_document(doc)

    def _chunk(index: int, level: HierarchyLevel) -> Chunk:
        return Chunk(
            chunk_id=f"test-lc-c{index}",
            document_id="test-d1",
            source_id="test-s1",
            hierarchy_level=level,
            sequence_index=index,
            text=f"chunk {index}",
            token_count=2,
            element_ids=[f"e{index}"],
            page_range=(index, index) if index % 2 == 0 else None,
        )

    # Inserted out of order and with a mix of levels - the adaptive chunker
    # emits leaf units only, so a document's chunks across levels are one
    # sequence and the default (no level filter) must return all of them.
    await store.save_chunks([_chunk(2, HierarchyLevel.SENTENCE), _chunk(0, HierarchyLevel.PARAGRAPH), _chunk(1, HierarchyLevel.PARAGRAPH)])

    chunks = await store.list_chunks_by_document("test-d1")
    assert [c.sequence_index for c in chunks] == [0, 1, 2]
    assert [c.hierarchy_level for c in chunks] == [HierarchyLevel.PARAGRAPH, HierarchyLevel.PARAGRAPH, HierarchyLevel.SENTENCE]
    assert chunks[0].page_range == (0, 0)
    assert chunks[1].page_range is None
    assert chunks[0].element_ids == ["e0"]
    assert sum(c.token_count for c in chunks) == 6

    only_paragraphs = await store.list_chunks_by_document("test-d1", hierarchy_level=HierarchyLevel.PARAGRAPH)
    assert [c.sequence_index for c in only_paragraphs] == [0, 1]

    assert await store.list_chunks_by_document("test-does-not-exist") == []


async def test_save_chunks_with_document_id_replaces_the_documents_chunk_set(store):
    """재청킹으로 같은 document_id가 더 적은 청크로 다시 나오면 예전 청크와 그 청크-grain
    임베딩이 남아 검색에 계속 걸리던 구멍(wiki/reingest-replay.md) - `document_id`를 주면
    그 문서의 청크 집합이 통째로 교체된다. KO evidence는 FK가 아닌 JSON 참조라 지워진
    청크를 가리킨 채 남는다."""
    src, doc, _, objects, _ = _sample_records()
    await store.save_source(src, None)
    await store.save_document(doc)

    def _chunk(index: int, text: str) -> Chunk:
        return Chunk(
            chunk_id=f"test-rc-c{index}",
            document_id="test-d1",
            source_id="test-s1",
            hierarchy_level=HierarchyLevel.PARAGRAPH,
            sequence_index=index,
            text=text,
            token_count=1,
        )

    old = [_chunk(i, f"old {i}") for i in range(3)]
    await store.save_chunks(old, document_id="test-d1")
    await store.save_embeddings([_chunk_record(c, src.project_id, [0.1] * 1536, text=c.text) for c in old])
    evidence_ko = objects[0].model_copy(
        update={"evidence": [objects[0].evidence[0].model_copy(update={"chunk_id": "test-rc-c2"})]}
    )
    await store.save_knowledge_objects([evidence_ko])

    # A plain upsert (no document_id) still never deletes.
    await store.save_chunks([_chunk(0, "old 0")])
    assert len(await store.list_chunks_by_document("test-d1")) == 3

    await store.save_chunks([_chunk(0, "merged")], document_id="test-d1")

    chunks = await store.list_chunks_by_document("test-d1")
    assert [(c.chunk_id, c.text) for c in chunks] == [("test-rc-c0", "merged")]
    rows = await (await store.conn.execute(
        "SELECT chunk_id FROM knowledge_object_embeddings WHERE document_id = 'test-d1' AND target_kind = 'chunk'"
    )).fetchall()
    assert rows == [("test-rc-c0",)]
    kept = await store.get_knowledge_object(evidence_ko.knowledge_object_id)
    assert kept is not None and kept.evidence[0].chunk_id == "test-rc-c2"

    await store.save_chunks([], document_id="test-d1")
    assert await store.list_chunks_by_document("test-d1") == []
