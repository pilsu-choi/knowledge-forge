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
    # Required (and only meaningful) when source == "local" (the default) -
    # a SaaS source is built from KF_* env config instead
    # (collectors/saas/factory.py), so `path` is ignored for those.
    path: str | None = None
    # "local" (default) or one of collectors.saas.factory.SAAS_COLLECTOR_BUILDERS
    # (currently "slack"/"notion"/"gdrive"/"gmail") - mirrors cli.py extract/
    # schedule's --source. Validated against that mapping in the route, not
    # here, so this schema doesn't need to import factory.py's SDK-adjacent
    # module just to spell out a Literal.
    source: str = "local"
    max_tokens: int | None = None
    model: str | None = None
    # Optional, like extract/schedule's --okf-dir: also write a file-based
    # OKF Knowledge Base tree (manifest.yaml + resources/objects/relations)
    # to this directory, under a {project_id}/ subfolder - independent of
    # Postgres storage (always on for a job). worker.py's _dispatch() builds
    # the OKFWriter and writes the manifest/views once, at the end of the
    # job (a job is a one-shot run, unlike `schedule`'s repeating tick).
    okf_dir: str | None = None
    # Tenancy key this ingest run's Source/Document/Job rows are tagged
    # with. Defaults to "default" for single-tenant callers.
    project_id: str = "default"


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
    project_id: str


class SourceResponse(BaseModel):
    source_id: str
    source_type: str
    uri: str
    content_hash: str
    collected_at: datetime
    modified_at: datetime | None = None
    metadata: dict[str, Any]
    project_id: str


class DocumentResponse(BaseModel):
    document_id: str
    source_id: str
    project_id: str
    metadata: dict[str, Any]
    elements: list[dict[str, Any]]


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
