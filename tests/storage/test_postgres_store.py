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
        await db.conn.execute("DELETE FROM knowledge_object_embeddings WHERE knowledge_object_id LIKE 'test-%'")
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
        knowledge_object_id=ko_id,
        embedding_model="test-model",
        embedding=[0.1] * 1536,
        embedded_text="Acme",
        embedded_text_hash="hash1",
        project_id=src.project_id,
    )
    await store.save_embeddings([record])

    updated = EmbeddingRecord(
        knowledge_object_id=ko_id,
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
    await store.save_source(src, raw_path=None)
    await store.save_document(doc)
    await store.save_chunks([chunk])
    await store.save_knowledge_objects(objects)

    entity_id = objects[0].knowledge_object_id
    fact_id = objects[1].knowledge_object_id

    # Neither KO has an embedding yet - both should show up as missing.
    missing = await store.list_knowledge_objects_missing_embedding("test-model", None, 50)
    missing_ids = {ko.knowledge_object_id for ko in missing}
    assert entity_id in missing_ids
    assert fact_id in missing_ids

    # Embed one of them - it should drop out of the missing list.
    await store.save_embeddings(
        [
            EmbeddingRecord(
                knowledge_object_id=entity_id,
                embedding_model="test-model",
                embedding=[0.1] * 1536,
                embedded_text="Acme",
                embedded_text_hash="hash1",
                project_id=src.project_id,
            )
        ]
    )

    missing = await store.list_knowledge_objects_missing_embedding("test-model", None, 50)
    missing_ids = {ko.knowledge_object_id for ko in missing}
    assert entity_id not in missing_ids
    assert fact_id in missing_ids

    # A different embedding_model doesn't count as "embedded" for this model.
    missing_other_model = await store.list_knowledge_objects_missing_embedding("other-model", None, 50)
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


async def test_search_knowledge_objects_by_embedding_orders_by_similarity_and_filters(store):
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
                knowledge_object_id=entity_id,
                embedding_model="test-model",
                embedding=vector_a,
                embedded_text="Acme",
                embedded_text_hash="hash1",
                project_id=src.project_id,
            ),
            EmbeddingRecord(
                knowledge_object_id=fact_id,
                embedding_model="test-model",
                embedding=vector_b,
                embedded_text="Acme owns X",
                embedded_text_hash="hash2",
                project_id=src.project_id,
            ),
        ]
    )

    results = await store.search_knowledge_objects_by_embedding(vector_a, "test-model", None, None, 10)
    assert [r["knowledge_object_id"] for r in results] == [entity_id, fact_id]
    assert results[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert results[1]["score"] == pytest.approx(0.0, abs=1e-6)

    fact_only = await store.search_knowledge_objects_by_embedding(vector_a, "test-model", None, "fact", 10)
    assert [r["knowledge_object_id"] for r in fact_only] == [fact_id]

    wrong_project = await store.search_knowledge_objects_by_embedding(
        vector_a, "test-model", "no-such-project", None, 10
    )
    assert wrong_project == []

    other_model = await store.search_knowledge_objects_by_embedding(vector_a, "other-model", None, None, 10)
    assert other_model == []


async def test_search_knowledge_objects_by_embedding_filters_by_the_sources_content_timestamp(store):
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
                knowledge_object_id=old_ko.knowledge_object_id,
                embedding_model="test-model",
                embedding=vector,
                embedded_text="old",
                embedded_text_hash="hash-old",
                project_id=old_src.project_id,
            ),
            EmbeddingRecord(
                knowledge_object_id=new_ko.knowledge_object_id,
                embedding_model="test-model",
                embedding=vector,
                embedded_text="new",
                embedded_text_hash="hash-new",
                project_id=new_src.project_id,
            ),
        ]
    )

    since_only = await store.search_knowledge_objects_by_embedding(
        vector, "test-model", None, None, 10, since=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert {r["knowledge_object_id"] for r in since_only} == {new_ko.knowledge_object_id}
    assert since_only[0]["content_at"] == "2026-09-01T00:00:00+00:00"

    until_only = await store.search_knowledge_objects_by_embedding(
        vector, "test-model", None, None, 10, until=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert {r["knowledge_object_id"] for r in until_only} == {old_ko.knowledge_object_id}

    both_bounds = await store.search_knowledge_objects_by_embedding(
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
                        knowledge_object_id=ko_id,
                        embedding_model="test-model",
                        embedding=vector,
                        embedded_text="Acme",
                        embedded_text_hash="hash1",
                        project_id=src.project_id,
                    )
                ]
            )
            results = await store.search_knowledge_objects_by_embedding(vector, "test-model", None, None, 10)
            assert ko_id in {r["knowledge_object_id"] for r in results}
    finally:
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {fresh_db}")
        await admin_conn.close()
