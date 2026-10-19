
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from knowledge_forge.api.deps import get_read_store
from knowledge_forge.api.schemas import KnowledgeObjectResponse, Page, RelationResponse
from knowledge_forge.search import graph
from knowledge_forge.storage.postgres_store import PostgresStore

router = APIRouter()


def _ko_to_response(ko) -> KnowledgeObjectResponse:
    return KnowledgeObjectResponse(
        knowledge_object_id=ko.knowledge_object_id,
        type=ko.type.value,
        source_id=ko.source.source_id,
        document_id=ko.document.document_id,
        summary=ko.summary,
        content=ko.content,
        topics=ko.topics,
        document_type=ko.document_type,
        extracted_at=ko.extracted_at,
        confidence=ko.confidence,
        version=ko.version,
        supersedes=ko.supersedes,
    )


def _rel_to_response(rel) -> RelationResponse:
    return RelationResponse(
        relation_id=rel.relation_id,
        type=rel.type.value,
        subject_id=rel.subject_id,
        object_id=rel.object_id,
        label=rel.label,
        confidence=rel.confidence,
        created_at=rel.created_at,
    )


@router.get(
    "/knowledge-objects",
    response_model=Page[KnowledgeObjectResponse],
    tags=["Knowledge Objects"],
    summary="List knowledge objects",
)
async def list_knowledge_objects(
    source_id: str | None = None,
    type: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    store: PostgresStore = Depends(get_read_store),
) -> Page[KnowledgeObjectResponse]:
    objects = await store.list_knowledge_objects(
        source_id=source_id, type=type, limit=limit, offset=offset, project_id=project_id
    )
    return Page(items=[_ko_to_response(o) for o in objects], limit=limit, offset=offset)


@router.get(
    "/knowledge-objects/{knowledge_object_id}",
    response_model=KnowledgeObjectResponse,
    tags=["Knowledge Objects"],
    summary="Get a knowledge object by id",
    responses={404: {"description": "knowledge object not found"}},
)
async def get_knowledge_object(
    knowledge_object_id: str, project_id: str | None = None, store: PostgresStore = Depends(get_read_store)
) -> KnowledgeObjectResponse:
    ko = await store.get_knowledge_object(knowledge_object_id, project_id=project_id)
    if ko is None:
        raise HTTPException(status_code=404, detail=f"knowledge object not found: {knowledge_object_id}")
    return _ko_to_response(ko)


@router.get(
    "/relations",
    response_model=Page[RelationResponse],
    tags=["Relations"],
    summary="List relations",
)
async def list_relations(
    subject_id: str | None = None,
    object_id: str | None = None,
    type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    store: PostgresStore = Depends(get_read_store),
) -> Page[RelationResponse]:
    relations = await store.list_relations(
        subject_id=subject_id, object_id=object_id, type=type, limit=limit, offset=offset
    )
    return Page(items=[_rel_to_response(r) for r in relations], limit=limit, offset=offset)


@router.get(
    "/relations/expand",
    tags=["Relations"],
    summary="Walk relations from a knowledge object",
    responses={400: {"description": "unknown relation type"}, 404: {"description": "knowledge object not found"}},
)
async def expand_relations(
    object_id: str,
    depth: int = 1,
    relation_types: list[str] | None = Query(None),
    project_id: str | None = None,
    limit: int = 50,
    store: PostgresStore = Depends(get_read_store),
) -> dict[str, Any]:
    """`search/graph.py::neighbors` - both directions, at most 3 hops, never
    leaves the start object's project, superseded objects excluded."""
    try:
        result = await graph.neighbors(
            store, object_id, project_id=project_id, depth=depth, relation_types=relation_types, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"knowledge object not found: {object_id}")
    return result


@router.get("/entities", tags=["Entities"], summary="Look up resolved entities by name")
async def lookup_entities(
    name: str,
    project_id: str | None = None,
    entity_type: str | None = None,
    limit: int = 10,
    store: PostgresStore = Depends(get_read_store),
) -> list[dict[str, Any]]:
    """`search/graph.py::lookup_entity` - each entity with the current objects that mention it."""
    return await graph.lookup_entity(store, name, project_id=project_id, entity_type=entity_type, limit=min(limit, 50))
