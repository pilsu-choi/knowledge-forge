"""The Knowledge Object model.

Design note (supersedes the earlier "PRD section 4 JSON shape" reading -
see wiki/knowledge-object-schema.md for the full rationale): a chunk does
not extract into exactly one KnowledgeObject. A single chunk of Slack
conversation can contain a Decision, its Reason, and an ActionItem as three
separate, independently addressable objects; conversely a Decision can be
grounded in more than one chunk. So:

- `KnowledgeObject` is a generic typed envelope (`type` discriminates
  Entity/Fact/Event/Decision/ActionItem/Requirement/Policy/Issue/Concept,
  see `KnowledgeObjectType`), not a single fixed bag of
  summary/facts/entities/relations fields.
- `evidence` is a list, not a single ChunkRef - cardinality between Chunk
  and KnowledgeObject is many-to-many ("Chunk supports KnowledgeObject"),
  not the 1:1 the earlier schema assumed.
- Cross-object relationships (a Decision's Reason, a Fact's mentioned
  Entity, ...) are `Relation` records (models/relation.py) linking
  `knowledge_object_id`s, not text embedded in this model.

`source`/`document` stay lightweight refs (ids + a few identifying fields),
not full embedded records, for the same reason as before: a Document can
back many chunks and many KnowledgeObjects, so embedding it per-object
would duplicate that data. Full records stay addressable by id through the
pipeline; provenance is preserved by id-chaining
(source_id -> document_id -> chunk_id(s) -> knowledge_object_id).
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.source import SourceRef


class KnowledgeObjectType(str, Enum):
    """Open-ended on purpose: this is the "generic envelope" ontology
    described in wiki/architecture.md - new types extend this enum rather
    than requiring a new top-level Pydantic model."""

    ENTITY = "entity"
    FACT = "fact"
    EVENT = "event"
    DECISION = "decision"
    ACTION_ITEM = "action_item"
    REQUIREMENT = "requirement"
    POLICY = "policy"
    ISSUE = "issue"
    CONCEPT = "concept"


class DocumentRef(BaseModel):
    document_id: str
    title: str | None = None


class Evidence(BaseModel):
    """One chunk that supports a KnowledgeObject. A KnowledgeObject holds a
    list of these (not a single ChunkRef) so multiple chunks can jointly
    support one object, and the same chunk can support multiple objects."""

    chunk_id: str
    document_id: str
    hierarchy_level: HierarchyLevel
    sequence_index: int


# `content` of an ENTITY KnowledgeObject - one *mention-level* extraction
# ("정민찬 팀장" as named in one chunk bundle); the resolved, cross-document
# node it points at is models/entity.py::Entity. Doubles as the LLM-facing
# `ExtractionResult.entities[]` shape (extraction/schemas.py), so the stored
# payload is exactly what the model returned. A comment, not a docstring:
# Pydantic would ship a docstring to the LLM as the schema description.
class EntityContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    entity_type: str = Field(description="PERSON, ORGANIZATION, PRODUCT, LOCATION, PROJECT, DATE, AMOUNT, ...")
    description: str | None = None


class StatementContent(BaseModel):
    """Base of the typed item contents below. `statement` stays on every
    item type so readers that only know `content["statement"]` (OKF
    writer, embedding text) keep working; fact/requirement/policy/issue/
    concept still store just `{"statement": ...}`, unvalidated."""

    model_config = ConfigDict(extra="forbid")

    statement: str


DecisionStatus = Literal["proposed", "decided", "rejected"]
ActionItemStatus = Literal["open", "in_progress", "done", "cancelled"]


class DecisionContent(StatementContent):
    decided_by: list[str] = Field(default_factory=list)
    # Dates stay text: "YYYY-MM-DD" when the source states an absolute date,
    # otherwise the expression as written ("다음 주 월요일") - resolving a
    # relative date needs the document's own date, which extraction
    # doesn't reliably have.
    decided_at: str | None = None
    status: DecisionStatus | None = None


class ActionItemContent(StatementContent):
    assignees: list[str] = Field(default_factory=list)
    due: str | None = None
    status: ActionItemStatus | None = None


class EventContent(StatementContent):
    occurred_at: str | None = None
    participants: list[str] = Field(default_factory=list)


CONTENT_MODELS: dict[KnowledgeObjectType, type[BaseModel]] = {
    KnowledgeObjectType.ENTITY: EntityContent,
    KnowledgeObjectType.DECISION: DecisionContent,
    KnowledgeObjectType.ACTION_ITEM: ActionItemContent,
    KnowledgeObjectType.EVENT: EventContent,
}


def validate_content(type_: KnowledgeObjectType, content: dict[str, Any]) -> dict[str, Any]:
    """The storage-time check (PostgresStore.save_knowledge_objects): the
    typed models above for the types that have one (raises
    pydantic.ValidationError on a mismatch), `content` unchanged for the
    rest - their shape is still free-form on purpose."""
    model = CONTENT_MODELS.get(type_)
    if model is None:
        return content
    return model.model_validate(content).model_dump(mode="json")


class KnowledgeObject(BaseModel):
    knowledge_object_id: str
    type: KnowledgeObjectType
    source: SourceRef
    document: DocumentRef
    evidence: list[Evidence] = Field(default_factory=list)
    summary: str
    # Type-specific payload, shaped by CONTENT_MODELS for the types that
    # have a model and `{"statement": ...}` for the rest. Kept a dict (not
    # a discriminated union) so new types from KnowledgeObjectType don't
    # require a schema migration here; validated at storage time instead
    # (`validate_content`).
    content: dict[str, Any] = Field(default_factory=dict)
    topics: list[str] = Field(default_factory=list)
    document_type: str
    extracted_at: datetime
    model_used: str
    # The extractor's own 0-1 confidence for this item (O5) - None for
    # entities and the summary fallback, which the LLM doesn't score.
    confidence: float | None = None
    # Versioning (wiki/architecture.md "Layer 5" / roadmap "재처리"): lets a
    # re-extraction (new pipeline_version or changed source content) be
    # recorded as a new object that supersedes the old one instead of a
    # silent overwrite - see extraction/claude_extractor.py.
    version: int = 1
    pipeline_version: str
    source_hash: str
    supersedes: str | None = None
