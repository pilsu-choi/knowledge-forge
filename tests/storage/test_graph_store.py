"""Graph reads (PostgresStore.neighbors / list_entity_objects, project-scoped
KO reads) and the MCP server built on them, against a real PostgreSQL -
skipped unless KF_TEST_DATABASE_URL is set."""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError

from knowledge_forge.entity_resolution import resolve_entities
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import DocumentRef, Evidence, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.search import graph
from knowledge_forge.storage.postgres_store import PostgresStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

_A, _B = "test-graph-a", "test-graph-b"


def _source(project_id: str) -> Source:
    return Source(
        source_id=f"test-graph-src-{project_id}",
        source_type=SourceType.SLACK,
        uri=f"slack://{project_id}",
        content_hash="h",
        collected_at=datetime.now(timezone.utc),
        project_id=project_id,
    )


def _ko(ko_id: str, type_=KnowledgeObjectType.FACT, project_id: str = _A, content=None, supersedes=None):
    return KnowledgeObject(
        knowledge_object_id=f"test-graph-{ko_id}",
        type=type_,
        source=SourceRef.from_source(_source(project_id)),
        document=DocumentRef(document_id=f"test-graph-doc-{project_id}"),
        evidence=[Evidence(chunk_id=f"c-{ko_id}", document_id=f"test-graph-doc-{project_id}",
                           hierarchy_level=HierarchyLevel.PARAGRAPH, sequence_index=0)],
        summary=ko_id,
        content=content if content is not None else {"statement": ko_id},
        document_type="slack",
        extracted_at=datetime.now(timezone.utc),
        model_used="m",
        pipeline_version="0",
        source_hash="h",
        supersedes=f"test-graph-{supersedes}" if supersedes else None,
    )


def _rel(rel_id: str, type_: RelationType, subject: str, obj: str, confidence=None) -> Relation:
    return Relation(
        relation_id=f"test-graph-{rel_id}",
        type=type_,
        subject_id=f"test-graph-{subject}",
        object_id=f"test-graph-{obj}",
        confidence=confidence,
        created_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def store():
    """issue <-caused_by- decision -mentions-> person(entity)
    decision -related_to-> old (superseded by new)
    issue -related_to-> far, and a cross-project edge issue -> other (project B)
    decision -duplicate_of-> dup"""
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        for project_id in (_A, _B):
            src = _source(project_id)
            await db.save_source(src, raw_path=None)
            await db.save_document(
                Document(
                    document_id=f"test-graph-doc-{project_id}",
                    source=SourceRef.from_source(src),
                    elements=[TextBlock(element_id="e1", text="x")],
                    metadata=DocumentMetadata(title="T"),
                )
            )
        person = _ko("person", KnowledgeObjectType.ENTITY, content={"name": "정민찬", "entity_type": "PERSON"})
        decision = _ko("decision", KnowledgeObjectType.DECISION, content={"statement": "d", "decided_by": ["정민찬 팀장"]})
        objects = [
            person, decision, _ko("issue", KnowledgeObjectType.ISSUE), _ko("old"), _ko("new", supersedes="old"),
            _ko("far"), _ko("dup"), _ko("other", project_id=_B),
        ]
        relations = [
            _rel("r1", RelationType.CAUSED_BY, "decision", "issue", confidence=0.9),
            _rel("r2", RelationType.MENTIONS, "decision", "person"),
            _rel("r3", RelationType.RELATED_TO, "decision", "old"),
            _rel("r4", RelationType.RELATED_TO, "issue", "far"),
            _rel("r5", RelationType.RELATED_TO, "issue", "other"),
            _rel("r6", RelationType.DUPLICATE_OF, "dup", "decision"),
            _rel("r7", RelationType.SUPERSEDES, "new", "old"),
        ]
        await db.save_knowledge_objects(objects)
        await db.save_relations(relations)
        resolution = resolve_entities(objects, relations)
        await db.save_entities(resolution.entities, resolution.mentions)
        yield db
        await db.conn.execute("DELETE FROM entities WHERE project_id = ANY(%s)", ([_A, _B],))
        await db.conn.execute("DELETE FROM relations WHERE relation_id LIKE 'test-graph-%'")
        await db.conn.execute("DELETE FROM knowledge_objects WHERE knowledge_object_id LIKE 'test-graph-%'")
        await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-graph-%'")
        await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-graph-%'")


def _ids(found) -> dict[str, int]:
    return {n.knowledge_object.knowledge_object_id.removeprefix("test-graph-"): n.depth for n in found}


async def test_neighbors_walks_both_directions_by_depth(store):
    _, one_hop = await store.neighbors("test-graph-issue", depth=1)
    assert _ids(one_hop) == {"decision": 1, "far": 1}  # incoming caused_by + outgoing related_to

    _, two_hops = await store.neighbors("test-graph-issue", depth=2)
    assert _ids(two_hops) == {"decision": 1, "far": 1, "person": 2}


async def test_neighbors_never_leaves_the_project_and_skips_superseded_and_bookkeeping_edges(store):
    _, found = await store.neighbors("test-graph-decision", depth=3)
    # "other" is project B, "old" is superseded, "dup" is only reachable via duplicate_of.
    assert set(_ids(found)) == {"issue", "person", "far"}


async def test_neighbors_relation_type_filter_and_scope(store):
    _, found = await store.neighbors("test-graph-decision", relation_types=["caused_by"])
    assert _ids(found) == {"issue": 1}
    assert await store.neighbors("test-graph-decision", project_id=_B) is None
    assert await store.neighbors("test-graph-missing") is None


async def test_entity_expansion_returns_current_objects_with_roles(store):
    (entity,) = await store.find_entities(_A, "정민찬")
    rows = await store.list_entity_objects(entity.entity_id)
    assert {(role, ko.knowledge_object_id) for role, ko in rows} == {
        ("entity", "test-graph-person"),
        ("decided_by", "test-graph-decision"),
        ("mentions", "test-graph-decision"),
    }


async def test_knowledge_object_reads_honor_project_id(store):
    assert await store.get_knowledge_object("test-graph-other", project_id=_A) is None
    assert (await store.get_knowledge_object("test-graph-other", project_id=_B)) is not None
    listed = await store.list_knowledge_objects(project_id=_B, limit=500)
    assert {ko.knowledge_object_id for ko in listed} == {"test-graph-other"}


async def test_graph_service_payload_says_which_edge_reached_each_object(store):
    result = await graph.neighbors(store, "test-graph-issue", project_id=_A, depth=1)
    by_id = {n["object"]["knowledge_object_id"]: n for n in result["neighbors"]}
    assert by_id["test-graph-decision"]["direction"] == "in"  # decision --caused_by--> issue
    assert by_id["test-graph-far"]["direction"] == "out"
    assert by_id["test-graph-far"]["from"] == "test-graph-issue"
    with pytest.raises(ValueError):
        await graph.neighbors(store, "test-graph-issue", project_id=_A, relation_types=["nope"])


async def test_mcp_server_pins_every_tool_to_its_project(store):
    from knowledge_forge.mcp_server import build_server

    server, other = build_server(DSN, _A), build_server(DSN, _B)
    try:
        names = {t.name for t in await server.list_tools()}
        assert names == {"lookup_entity", "get_object", "neighbors", "search"}
        assert all("project_id" not in t.input_schema["properties"] for t in await server.list_tools())

        found = await server.call_tool("lookup_entity", {"name": "정민찬 팀장님"})
        (entity,) = found.structured_content["result"]
        assert entity["project_id"] == _A and len(entity["objects"]) == 3

        walk = await server.call_tool("neighbors", {"object_id": "test-graph-decision", "depth": 2})
        assert {n["object"]["knowledge_object_id"] for n in walk.structured_content["neighbors"]} == {
            "test-graph-issue", "test-graph-person", "test-graph-far",
        }
        # Another project's object reads as not found - there is no argument to widen the scope.
        with pytest.raises(ToolError):
            await server.call_tool("get_object", {"object_id": "test-graph-other"})
        got = await other.call_tool("get_object", {"object_id": "test-graph-other"})
        assert got.structured_content["project_id"] == _B
    finally:
        await server.aclose()
        await other.aclose()
