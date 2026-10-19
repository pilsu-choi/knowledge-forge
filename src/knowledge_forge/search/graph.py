"""Ontology reads - entity lookup, object fetch and relation walks
(wiki/improvement-plan-2026-09-15.md O3/O6).

Same layering rule as `search/document.py`: plain async functions returning
JSON-ready dicts, so the agent tools (agent/tools.py), the MCP server
(mcp_server.py) and the HTTP routes (api/routes/knowledge.py) are thin
adapters over one implementation. `store` is a Protocol because the HTTP
routes hold a `PostgresStore` and the agent/MCP a `PipelineStore`.

Every function takes the caller's `project_id` scope (None = unscoped, the
agent's default) and passes it down to storage, which enforces it - an
object or entity outside the scope reads as absent.
"""

from __future__ import annotations

from typing import Any, Protocol

from knowledge_forge.models.entity import Entity
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.relation import Neighbor, RelationType

MAX_DEPTH = 3
MAX_NEIGHBORS = 200


class GraphReadStore(Protocol):
    async def get_knowledge_object(
        self, knowledge_object_id: str, *, project_id: str | None = None
    ) -> KnowledgeObject | None: ...

    async def find_entities(
        self, project_id: str | None, name: str, *, entity_type: str | None = None, limit: int = 10
    ) -> list[Entity]: ...

    async def list_entity_objects(
        self, entity_id: str, *, roles: list[str] | None = None, limit: int = 50
    ) -> list[tuple[str, KnowledgeObject]]: ...

    async def neighbors(self, object_id: str, **kwargs: Any) -> tuple[KnowledgeObject, list[Neighbor]] | None: ...


def object_payload(ko: KnowledgeObject) -> dict[str, Any]:
    return {
        "knowledge_object_id": ko.knowledge_object_id,
        "type": ko.type.value,
        "summary": ko.summary,
        "content": ko.content,
        "confidence": ko.confidence,
        "document_id": ko.document.document_id,
        "source_id": ko.source.source_id,
        "source_uri": ko.source.uri,
        "project_id": ko.source.project_id,
        "chunk_ids": [e.chunk_id for e in ko.evidence],
        "extracted_at": ko.extracted_at.isoformat(),
        "supersedes": ko.supersedes,
    }


async def lookup_entity(
    store: GraphReadStore,
    name: str,
    *,
    project_id: str | None,
    entity_type: str | None = None,
    limit: int = 10,
    objects_per_entity: int = 20,
) -> list[dict[str, Any]]:
    """Entities matching `name` (normalized like resolution, or an exact
    alias), each expanded to the current objects that mention it."""
    entities = await store.find_entities(project_id, name, entity_type=entity_type, limit=limit)
    results = []
    for entity in entities:
        mentions = await store.list_entity_objects(entity.entity_id, limit=objects_per_entity)
        results.append(
            {
                **entity.model_dump(mode="json"),
                "objects": [{"role": role, **object_payload(ko)} for role, ko in mentions],
            }
        )
    return results


async def get_object(store: GraphReadStore, object_id: str, *, project_id: str | None) -> dict[str, Any] | None:
    ko = await store.get_knowledge_object(object_id, project_id=project_id)
    return object_payload(ko) if ko is not None else None


def parse_relation_types(values: list[str] | None) -> list[str] | None:
    """Validates caller-supplied relation type names (ValueError on an
    unknown one) - None keeps storage's traversal default."""
    if not values:
        return None
    known = {t.value for t in RelationType}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"unknown relation type(s): {', '.join(unknown)} - known: {', '.join(sorted(known))}")
    return list(values)


async def neighbors(
    store: GraphReadStore,
    object_id: str,
    *,
    project_id: str | None,
    depth: int = 1,
    relation_types: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any] | None:
    """The start object and what `depth` hops of relations reach, both
    directions. `depth`/`limit` are clamped to MAX_DEPTH/MAX_NEIGHBORS here
    so no adapter can ask storage for an unbounded walk. Each neighbor says
    which edge reached it from which object (`from`): `direction` "out"
    means `from --type--> object`, "in" means `object --type--> from`."""
    walk = await store.neighbors(
        object_id,
        project_id=project_id,
        depth=max(1, min(depth, MAX_DEPTH)),
        relation_types=parse_relation_types(relation_types),
        limit=max(1, min(limit, MAX_NEIGHBORS)),
    )
    if walk is None:
        return None
    start, found = walk
    items = []
    for n in found:
        rel = n.relation
        target = n.knowledge_object.knowledge_object_id
        items.append(
            {
                "depth": n.depth,
                "direction": "in" if rel.subject_id == target else "out",
                "from": rel.object_id if rel.subject_id == target else rel.subject_id,
                "relation": {
                    "relation_id": rel.relation_id,
                    "type": rel.type.value,
                    "label": rel.label,
                    "confidence": rel.confidence,
                },
                "object": object_payload(n.knowledge_object),
            }
        )
    return {"object": object_payload(start), "neighbors": items}
