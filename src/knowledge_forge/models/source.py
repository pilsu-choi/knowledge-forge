from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    LOCAL_FILE = "local_file"
    SLACK = "slack"
    GMAIL = "gmail"
    GOOGLE_DRIVE = "google_drive"
    NOTION = "notion"


class Source(BaseModel):
    """A single collected item and everything needed to trace it back to its origin."""

    source_id: str
    source_type: SourceType
    uri: str
    content_hash: str
    collected_at: datetime
    modified_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Tenancy key: which project this Source belongs to. Defaults to
    # "default" so existing single-tenant callers (tests, parsers) keep
    # working unchanged; real ingestion entry points (CLI/API) pass an
    # explicit value through pipeline.run_pipeline(project_id=...).
    project_id: str = "default"


class SourceRef(BaseModel):
    """Lightweight provenance pointer to a Source, embedded in downstream objects."""

    source_id: str
    source_type: SourceType
    uri: str
    project_id: str = "default"

    @classmethod
    def from_source(cls, source: Source) -> "SourceRef":
        return cls(
            source_id=source.source_id,
            source_type=source.source_type,
            uri=source.uri,
            project_id=source.project_id,
        )
