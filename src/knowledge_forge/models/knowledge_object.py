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
from typing import Any

from pydantic import BaseModel, Field

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


class Entity(BaseModel):
    """Value-object shape for an entity's own attributes. Used as the
    `content` payload of an ENTITY-typed KnowledgeObject - kept as a
    separate model (rather than inlined) so it stays reusable/typed at the
    call site (extraction, tests) instead of a bare dict."""

    name: str
    type: str
    description: str | None = None


class KnowledgeObject(BaseModel):
    knowledge_object_id: str
    type: KnowledgeObjectType
    source: SourceRef
    document: DocumentRef
    evidence: list[Evidence] = Field(default_factory=list)
    summary: str
    # Type-specific payload: {"name", "entity_type", "description"} for
    # ENTITY, {"statement": ...} for FACT/EVENT/DECISION/ACTION_ITEM/... .
    # A dict (not a discriminated union of per-type models) so new types
    # from KnowledgeObjectType don't require a schema migration here.
    content: dict[str, Any] = Field(default_factory=dict)
    topics: list[str] = Field(default_factory=list)
    document_type: str
    extracted_at: datetime
    model_used: str
    # Versioning (wiki/architecture.md "Layer 5" / roadmap "재처리"): lets a
    # re-extraction (new pipeline_version or changed source content) be
    # recorded as a new object that supersedes the old one instead of a
    # silent overwrite - see extraction/claude_extractor.py.
    version: int = 1
    pipeline_version: str
    source_hash: str
    supersedes: str | None = None
