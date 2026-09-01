"""The LLM extraction output shape.

This model IS the Anthropic structured-output schema passed as
`output_format=ExtractionResult` to `client.messages.parse(...)` - no
separate hand-maintained JSON schema to keep in sync.
"""

from pydantic import BaseModel, Field


class ExtractionEntity(BaseModel):
    name: str
    type: str
    description: str | None = None


class ExtractionRelation(BaseModel):
    subject: str
    predicate: str
    object: str
    confidence: float | None = None


class ExtractionResult(BaseModel):
    summary: str
    facts: list[str] = Field(default_factory=list)
    entities: list[ExtractionEntity] = Field(default_factory=list)
    relations: list[ExtractionRelation] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    document_type: str
