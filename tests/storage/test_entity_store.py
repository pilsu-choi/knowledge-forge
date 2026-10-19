"""Entity tables (entities / entity_mentions) and the O2/O5 knowledge_objects
changes against a real PostgreSQL - skipped unless KF_TEST_DATABASE_URL is set,
same convention as test_postgres_store.py."""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from pydantic import ValidationError

from knowledge_forge.entity_resolution import resolve_entities
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.entity import MentionRole
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.postgres_store import PostgresStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

_PROJECTS = ("test-onto-a", "test-onto-b")


def _source(project_id: str) -> Source:
    return Source(
        source_id=f"test-ent-src-{project_id}",
        source_type=SourceType.SLACK,
        uri=f"slack://{project_id}",
        content_hash="h",
        collected_at=datetime.now(timezone.utc),
        project_id=project_id,
    )


def _ko(ko_id: str, type_: KnowledgeObjectType, content: dict, project_id: str, confidence=None) -> KnowledgeObject:
    src = _source(project_id)
    return KnowledgeObject(
        knowledge_object_id=f"test-ent-{ko_id}",
        type=type_,
        source=SourceRef.from_source(src),
        document=DocumentRef(document_id=f"test-ent-doc-{project_id}"),
        evidence=[Evidence(chunk_id="test-ent-c1", document_id=f"test-ent-doc-{project_id}",
                           hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary="s",
        content=content,
        document_type="slack",
        extracted_at=datetime.now(timezone.utc),
        model_used="m",
        confidence=confidence,
        pipeline_version="0",
        source_hash="h",
    )


@pytest_asyncio.fixture
async def store():
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        for project_id in _PROJECTS:
            src = _source(project_id)
            await db.save_source(src, raw_path=None)
            await db.save_document(
                Document(
                    document_id=f"test-ent-doc-{project_id}",
                    source=SourceRef.from_source(src),
                    elements=[TextBlock(element_id="e1", text="x")],
                    metadata=DocumentMetadata(title="T"),
                )
            )
        yield db
        await db.conn.execute("DELETE FROM entities WHERE project_id = ANY(%s)", (list(_PROJECTS),))
        await db.conn.execute("DELETE FROM knowledge_objects WHERE knowledge_object_id LIKE 'test-ent-%'")
        await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-ent-%'")
        await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-ent-%'")


async def _save(db: PostgresStore, objects: list[KnowledgeObject]) -> None:
    await db.save_knowledge_objects(objects)
    resolution = resolve_entities(objects, [])
    await db.save_entities(resolution.entities, resolution.mentions)


async def test_entities_upsert_merges_aliases_across_batches_and_stays_idempotent(store):
    first = [
        _ko("e1", KnowledgeObjectType.ENTITY, {"name": "정민찬", "entity_type": "PERSON"}, "test-onto-a"),
        _ko("dec", KnowledgeObjectType.DECISION, {"statement": "s", "decided_by": ["정민찬 팀장"]}, "test-onto-a"),
    ]
    second = [_ko("e2", KnowledgeObjectType.ENTITY, {"name": "정민찬 팀장님", "entity_type": "인물"}, "test-onto-a")]
    for _ in range(2):
        await _save(store, first)
        await _save(store, second)

    (entity,) = await store.find_entities("test-onto-a", "정민찬")
    assert entity.display_name == "정민찬"
    assert entity.aliases == ["정민찬", "정민찬 팀장", "정민찬 팀장님"]
    mentions = await store.list_entity_mentions(entity.entity_id)
    assert {(m.knowledge_object_id, m.role) for m in mentions} == {
        ("test-ent-e1", MentionRole.ENTITY),
        ("test-ent-dec", MentionRole.DECIDED_BY),
        ("test-ent-e2", MentionRole.ENTITY),
    }
    assert all(m.evidence == ["test-ent-c1"] for m in mentions)
    # A title-decorated query and an exact alias both find it.
    assert [e.entity_id for e in await store.find_entities("test-onto-a", "정민찬 팀장님")] == [entity.entity_id]


async def test_same_name_in_another_project_is_a_different_entity(store):
    await _save(store, [_ko("a", KnowledgeObjectType.ENTITY, {"name": "정민찬", "entity_type": "PERSON"}, "test-onto-a")])
    await _save(store, [_ko("b", KnowledgeObjectType.ENTITY, {"name": "정민찬", "entity_type": "PERSON"}, "test-onto-b")])

    (a,) = await store.find_entities("test-onto-a", "정민찬")
    (b,) = await store.find_entities("test-onto-b", "정민찬")
    assert a.entity_id != b.entity_id
    assert [m.knowledge_object_id for m in await store.list_entity_mentions(b.entity_id)] == ["test-ent-b"]


async def test_deleting_a_knowledge_object_cascades_its_mentions(store):
    await _save(store, [_ko("x", KnowledgeObjectType.ENTITY, {"name": "에이지소다", "entity_type": "ORG"}, "test-onto-a")])
    (entity,) = await store.find_entities("test-onto-a", "㈜에이지소다", entity_type="organization")

    await store.conn.execute("DELETE FROM knowledge_objects WHERE knowledge_object_id = 'test-ent-x'")

    assert await store.list_entity_mentions(entity.entity_id) == []


async def test_confidence_and_typed_content_round_trip(store):
    ko = _ko("act", KnowledgeObjectType.ACTION_ITEM, {"statement": "QA", "assignees": ["김철수"]}, "test-onto-a",
             confidence=0.75)
    await store.save_knowledge_objects([ko])

    fetched = await store.get_knowledge_object("test-ent-act")
    assert fetched.confidence == 0.75
    # Stored normalized: the typed model's defaults are filled in.
    assert fetched.content == {"statement": "QA", "assignees": ["김철수"], "due": None, "status": None}


async def test_malformed_typed_content_is_rejected_before_writing(store):
    bad = _ko("bad", KnowledgeObjectType.DECISION, {"statement": "s", "status": "done"}, "test-onto-a")
    with pytest.raises(ValidationError):
        await store.save_knowledge_objects([bad])
    assert await store.get_knowledge_object("test-ent-bad") is None


async def test_pipeline_resolves_extracted_people_into_entities(tmp_path):
    """End to end through run_pipeline + PipelineStore: an extraction naming the
    same person as an entity and as a decision's decider lands as one entity row
    with both mentions."""
    from knowledge_forge.chunking import AdaptiveChunker
    from knowledge_forge.collectors.local_file import LocalFileCollector
    from knowledge_forge.extraction.base import Extractor
    from knowledge_forge.extraction.outcome import build_extraction_outcome
    from knowledge_forge.extraction.schemas import ExtractionItem, ExtractionResult
    from knowledge_forge.models.knowledge_object import EntityContent
    from knowledge_forge.parsers.registry import default_registry
    from knowledge_forge.pipeline import run_pipeline
    from knowledge_forge.storage.store import PipelineStore

    class _Extractor(Extractor):
        async def extract(self, chunks, document, source):
            result = ExtractionResult(
                summary="s",
                entities=[EntityContent(name="정민찬 팀장", entity_type="PERSON")],
                items=[ExtractionItem(type="decision", statement="배포 연기", related_entities=["정민찬 팀장"],
                                      people=["정민찬"], status="decided", confidence=0.9)],
                document_type="note",
            )
            return build_extraction_outcome(result, chunks, document, source, model_used="fake")

    project_id = "test-onto-pipeline"
    doc_path = tmp_path / "meeting.txt"
    doc_path.write_text("정민찬 팀장: 배포는 월요일로 미룹니다.\n", encoding="utf-8")
    async with PipelineStore(DSN, tmp_path / "data") as pstore:
        await pstore.init_schema()
        try:
            outcomes = [
                o
                async for o in run_pipeline(
                    LocalFileCollector(root=doc_path), default_registry(), AdaptiveChunker(), _Extractor(),
                    store=pstore, project_id=project_id,
                )
            ]
            decision = next(ko for o in outcomes for ko in o.objects if ko.type == KnowledgeObjectType.DECISION)

            (entity,) = await pstore._db.find_entities(project_id, "정민찬")
            mentions = await pstore._db.list_entity_mentions(entity.entity_id)
            assert {m.role for m in mentions} == {MentionRole.ENTITY, MentionRole.MENTIONS, MentionRole.DECIDED_BY}
            assert {m.knowledge_object_id for m in mentions if m.role != MentionRole.ENTITY} == {
                decision.knowledge_object_id
            }
            stored = await pstore._db.get_knowledge_object(decision.knowledge_object_id)
            assert stored.confidence == 0.9
            assert stored.content["decided_by"] == ["정민찬"]
        finally:
            conn = pstore.conn
            await conn.execute("DELETE FROM entities WHERE project_id = %s", (project_id,))
            sources = "SELECT source_id FROM sources WHERE project_id = %(p)s"
            kos = f"SELECT knowledge_object_id FROM knowledge_objects WHERE source_id IN ({sources})"
            params = {"p": project_id}
            await conn.execute(f"DELETE FROM relations WHERE subject_id IN ({kos}) OR object_id IN ({kos})", params)
            await conn.execute(f"DELETE FROM knowledge_objects WHERE source_id IN ({sources})", params)
            await conn.execute(f"DELETE FROM chunks WHERE source_id IN ({sources})", params)
            docs = "SELECT document_id FROM documents WHERE project_id = %(p)s"
            await conn.execute(
                f"DELETE FROM document_links WHERE from_document_id IN ({docs}) OR to_document_id IN ({docs})", params
            )
            await conn.execute("DELETE FROM documents WHERE project_id = %(p)s", params)
            await conn.execute("DELETE FROM sources WHERE project_id = %(p)s", params)
