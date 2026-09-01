"""Request/response models for the API layer.

Kept separate from `knowledge_forge.storage.job_store.Job` and
`knowledge_forge.models.*` even where the shape is identical (`JobResponse`
mirrors `Job`) so the wire format can diverge from the internal/storage
model without touching persistence code - a deliberate boundary, not
duplication for its own sake.
"""

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from knowledge_forge.storage.job_store import JobStatus

T = TypeVar("T")


class IngestRequest(BaseModel):
    path: str


class IngestResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    job_type: str
    payload: dict
    status: JobStatus
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class KnowledgeObjectResponse(BaseModel):
    knowledge_object_id: str
    type: str
    source_id: str
    document_id: str
    summary: str
    content: dict[str, Any]
    topics: list[str]
    document_type: str
    extracted_at: datetime
    version: int
    supersedes: str | None = None


class RelationResponse(BaseModel):
    relation_id: str
    type: str
    subject_id: str
    object_id: str
    label: str | None = None
    confidence: float | None = None
    created_at: datetime


class Page(BaseModel, Generic[T]):
    items: list[T]
    limit: int
    offset: int
