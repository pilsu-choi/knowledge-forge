"""Relation: a typed edge between two KnowledgeObjects.

Per the reviewed architecture (wiki/architecture.md's "Layer 4 - Structure"),
relations connect KnowledgeObject ids directly - they are not free-text
subject/predicate/object strings embedded inside a single object. This is
what lets a Decision, its Reason, and the entities it mentions stay separate,
independently addressable objects while remaining connected as a graph.

Relation is deliberately a thin, generic edge model (not one Python class per
relation type) so new relation types can be added by extending RelationType
without touching this schema - mirrors how KnowledgeObjectType works.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class RelationType(str, Enum):
    """Extensible relation vocabulary. Extraction currently produces a
    subset of these (see extraction/outcome.py); the rest are reserved for
    later extraction passes or manual/derived relations (e.g. SUPERSEDES on
    re-extraction, see knowledge_object.py)."""

    MENTIONS = "mentions"  # a Fact/Decision/Event/... references an Entity
    RELATED_TO = "related_to"  # generic association (incl. entity-entity triples)
    CAUSED_BY = "caused_by"  # e.g. a Decision caused_by a Reason/Issue
    DEPENDS_ON = "depends_on"
    REFERENCES = "references"  # cites another object/document
    SUPERSEDES = "supersedes"  # a re-extraction replacing a prior object


class Relation(BaseModel):
    """A directed, typed edge: subject_id --type--> object_id.

    `subject_id`/`object_id` are `KnowledgeObject.knowledge_object_id`
    values. `label` preserves the original predicate text (e.g. "signed",
    "발행일로부터") when the relation was derived from an extracted
    subject-predicate-object triple, for readability - `type` is the
    normalized/queryable field.
    """

    relation_id: str
    type: RelationType
    subject_id: str
    object_id: str
    label: str | None = None
    confidence: float | None = None
    # chunk_ids whose text supports this relation - same provenance idea as
    # KnowledgeObject.evidence, kept separate here since one relation can be
    # grounded in a different (sub)set of chunks than either endpoint object.
    evidence: list[str] = Field(default_factory=list)
    created_at: datetime
