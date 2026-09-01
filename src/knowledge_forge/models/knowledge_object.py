"""The Knowledge Object model - PRD section 4's standardized output shape.

PRD section 4's illustrative JSON only shows
source/document/chunk/summary/facts/entities/relations, but section 3-4
explicitly requires Topic/Keyword and Document Type as extraction outputs.
We treat the section-4 JSON as illustrative rather than exhaustive and add
`topics`/`document_type` as top-level fields - see
wiki/knowledge-object-schema.md for the full rationale.

`source`/`document`/`chunk` here are lightweight refs (ids + a few
identifying fields), not full embedded records. Embedding the full Source/
Document into every chunk-level Knowledge Object would duplicate that data
across every chunk of a document. Full records stay addressable by id
through the pipeline; provenance is preserved by id-chaining
(source_id -> document_id -> chunk_id -> knowledge_object_id).
"""

from datetime import datetime

from pydantic import BaseModel, Field

from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.source import SourceRef


class DocumentRef(BaseModel):
    document_id: str
    title: str | None = None


class ChunkRef(BaseModel):
    chunk_id: str
    hierarchy_level: HierarchyLevel
    sequence_index: int


class Entity(BaseModel):
    name: str
    type: str
    description: str | None = None


class Relation(BaseModel):
    subject: str
    predicate: str
    object: str
    confidence: float | None = None


class KnowledgeObject(BaseModel):
    knowledge_object_id: str
    source: SourceRef
    document: DocumentRef
    chunk: ChunkRef
    summary: str
    facts: list[str] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    document_type: str
    extracted_at: datetime
    model_used: str
