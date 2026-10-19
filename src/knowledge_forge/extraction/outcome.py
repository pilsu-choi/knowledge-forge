"""Shared ExtractionResult -> ExtractionOutcome mapping.

Both provider extractors (claude_extractor.py, openrouter_extractor.py) call
the LLM differently and parse the raw response differently (parsed_output
vs. a raw JSON string to validate) - that part stays duplicated per
provider by design (see openrouter_extractor.py's docstring). But turning
the validated `ExtractionResult` into typed KnowledgeObjects + Relations is
provider-agnostic and non-trivial (entity/item id assignment, relation
resolution), so it lives here once instead of being duplicated per provider.
"""

from datetime import datetime, timezone
from typing import get_args

import knowledge_forge
from knowledge_forge.extraction.base import ExtractionOutcome
from knowledge_forge.extraction.schemas import ExtractionItem, ExtractionResult
from knowledge_forge.ids import stable_id
from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import (
    ActionItemContent,
    ActionItemStatus,
    DecisionContent,
    DecisionStatus,
    DocumentRef,
    EventContent,
    Evidence,
    KnowledgeObject,
    KnowledgeObjectType,
)
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import Source, SourceRef


def _valid_index(index: int, items: list[str], skip: int) -> bool:
    return 0 <= index < len(items) and index != skip


def _item_content(item: ExtractionItem) -> dict:
    """The flat `people`/`date`/`status` attributes mapped onto the item
    type's typed content model. A status from the other type's vocabulary
    (a decision marked "done") is dropped rather than stored."""
    if item.type == "decision":
        status = item.status if item.status in get_args(DecisionStatus) else None
        content = DecisionContent(statement=item.statement, decided_by=item.people, decided_at=item.date, status=status)
    elif item.type == "action_item":
        status = item.status if item.status in get_args(ActionItemStatus) else None
        content = ActionItemContent(statement=item.statement, assignees=item.people, due=item.date, status=status)
    elif item.type == "event":
        content = EventContent(statement=item.statement, occurred_at=item.date, participants=item.people)
    else:
        return {"statement": item.statement}
    return content.model_dump(mode="json")


def _confidence(value: float | None) -> float | None:
    return value if value is not None and 0.0 <= value <= 1.0 else None


def build_extraction_outcome(
    result: ExtractionResult, chunks: list[Chunk], document: Document, source: Source, model_used: str
) -> ExtractionOutcome:
    source_ref = SourceRef.from_source(source)
    document_ref = DocumentRef(document_id=document.document_id, title=document.metadata.title)
    evidence = [
        Evidence(
            chunk_id=chunk.chunk_id,
            document_id=document.document_id,
            hierarchy_level=chunk.hierarchy_level,
            sequence_index=chunk.sequence_index,
        )
        for chunk in chunks
    ]
    # A stable per-call id prefix - joins every chunk id in the bundle so a
    # different chunk grouping (or a single chunk moved into a different
    # bundle) always produces different KnowledgeObject ids, same as before
    # for a single-chunk group (bundle_id == the one chunk's id).
    bundle_id = stable_id(*(c.chunk_id for c in chunks))
    now = datetime.now(timezone.utc)

    def make_object(
        kind: str,
        kind_type: KnowledgeObjectType,
        index: int,
        summary: str,
        content: dict,
        confidence: float | None = None,
    ) -> KnowledgeObject:
        return KnowledgeObject(
            knowledge_object_id=stable_id(bundle_id, kind, str(index)),
            type=kind_type,
            source=source_ref,
            document=document_ref,
            evidence=evidence,
            summary=summary,
            content=content,
            topics=result.topics,
            document_type=result.document_type,
            extracted_at=now,
            model_used=model_used,
            confidence=confidence,
            pipeline_version=knowledge_forge.__version__,
            source_hash=source.content_hash,
        )

    objects: list[KnowledgeObject] = []
    relations: list[Relation] = []

    entity_ko_id_by_name: dict[str, str] = {}
    for i, entity in enumerate(result.entities):
        ko = make_object(
            "entity",
            KnowledgeObjectType.ENTITY,
            i,
            summary=entity.description or entity.name,
            content=entity.model_dump(mode="json"),
        )
        objects.append(ko)
        entity_ko_id_by_name[entity.name.strip().lower()] = ko.knowledge_object_id

    def relation(
        rel_type: RelationType,
        subject_id: str,
        object_id: str,
        label: str | None = None,
        confidence: float | None = None,
    ) -> Relation:
        return Relation(
            relation_id=stable_id(bundle_id, "rel", subject_id, object_id, rel_type.value),
            type=rel_type,
            subject_id=subject_id,
            object_id=object_id,
            label=label,
            confidence=confidence,
            evidence=[c.chunk_id for c in chunks],
            created_at=now,
        )

    # Entity-to-entity triples: only resolvable (and worth keeping as a
    # graph edge) when both names match an entity this same call extracted
    # - otherwise there's no KnowledgeObject id on either side to link.
    for r in result.relations:
        subject_ko_id = entity_ko_id_by_name.get(r.subject.strip().lower())
        object_ko_id = entity_ko_id_by_name.get(r.object.strip().lower())
        if subject_ko_id is None or object_ko_id is None:
            continue
        relations.append(
            relation(RelationType.RELATED_TO, subject_ko_id, object_ko_id, label=r.predicate, confidence=r.confidence)
        )

    item_ko_ids: list[str] = []
    for i, item in enumerate(result.items):
        ko = make_object(
            "item",
            KnowledgeObjectType(item.type),
            i,
            summary=item.statement,
            content=_item_content(item),
            confidence=_confidence(item.confidence),
        )
        objects.append(ko)
        item_ko_ids.append(ko.knowledge_object_id)

    for i, item in enumerate(result.items):
        item_ko_id = item_ko_ids[i]
        for name in item.related_entities:
            entity_ko_id = entity_ko_id_by_name.get(name.strip().lower())
            if entity_ko_id is not None:
                relations.append(relation(RelationType.MENTIONS, item_ko_id, entity_ko_id))
        if item.caused_by_index is not None and _valid_index(item.caused_by_index, item_ko_ids, skip=i):
            relations.append(relation(RelationType.CAUSED_BY, item_ko_id, item_ko_ids[item.caused_by_index]))
        for idx in item.relates_to_indices:
            if _valid_index(idx, item_ko_ids, skip=i):
                relations.append(relation(RelationType.RELATED_TO, item_ko_id, item_ko_ids[idx]))

    if not objects:
        # Nothing extractable as an entity or a typed item (e.g. a chunk
        # that's pure connective prose) - fall back to one FACT object from
        # the chunk summary so a chunk with content never silently yields
        # zero KnowledgeObjects.
        objects.append(
            make_object(
                "summary",
                KnowledgeObjectType.FACT,
                0,
                summary=result.summary,
                content={"statement": result.summary},
            )
        )

    return ExtractionOutcome(objects=objects, relations=relations)
