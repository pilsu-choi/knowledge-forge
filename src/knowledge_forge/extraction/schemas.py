"""The LLM extraction output shape.

This model IS the Anthropic structured-output schema passed as
`output_format=ExtractionResult` to `client.messages.parse(...)` - no
separate hand-maintained JSON schema to keep in sync.

`items` replaces the old flat `facts: list[str]` field: a chunk yields zero
or more typed knowledge items (Fact/Event/Decision/ActionItem/Requirement/
Policy/Issue/Concept - see ExtractionItemType), each becoming its own
KnowledgeObject (extraction/claude_extractor.py), instead of a single
KnowledgeObject per chunk holding a flat fact list (see
wiki/knowledge-object-schema.md for the rationale).
"""

from typing import Literal

from pydantic import BaseModel, Field

ExtractionItemType = Literal[
    "fact",
    "event",
    "decision",
    "action_item",
    "requirement",
    "policy",
    "issue",
    "concept",
]


class ExtractionEntity(BaseModel):
    name: str
    type: str
    description: str | None = None


class ExtractionRelation(BaseModel):
    """An entity-to-entity triple stated in the chunk text. Resolved into a
    graph Relation between the corresponding ENTITY KnowledgeObjects at
    extraction time (see claude_extractor._resolve_entity_relations) -
    subject/object here are entity *names*, not ids, since ids don't exist
    until after this LLM call assigns them."""

    subject: str
    predicate: str
    object: str
    confidence: float | None = None


class ExtractionItem(BaseModel):
    """One typed knowledge item extracted from the chunk. `caused_by_index`/
    `relates_to_indices` are 0-based indices into this same ExtractionResult's
    `items` list, letting the model state e.g. "this Decision is caused_by
    that Issue" without needing to know ids up front - claude_extractor
    resolves these into Relation records once ids are assigned."""

    type: ExtractionItemType
    statement: str = Field(description="The core content of this item, standalone and verifiable.")
    related_entities: list[str] = Field(
        default_factory=list,
        description="Names of entities (from the `entities` list) this item mentions or is about.",
    )
    caused_by_index: int | None = Field(
        default=None, description="Index into `items` of the item that caused/motivated this one, if any."
    )
    relates_to_indices: list[int] = Field(
        default_factory=list, description="Indices into `items` of other items this one is generically related to."
    )
    confidence: float | None = None


class ExtractionResult(BaseModel):
    summary: str
    entities: list[ExtractionEntity] = Field(default_factory=list)
    relations: list[ExtractionRelation] = Field(default_factory=list)
    items: list[ExtractionItem] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    document_type: str
