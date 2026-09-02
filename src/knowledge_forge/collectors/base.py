"""Collector interface.

Generic enough to cover both filesystem walks (File sources) and paginated
API polling (SaaS sources): `RawItem.deleted` expresses delete-detection
uniformly, `content_hash` expresses change-detection uniformly, and
`Cursor` is an opaque per-collector bookmark (a manifest checkpoint for
files, a page token / updated-after timestamp for SaaS APIs).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Iterator

from pydantic import BaseModel, Field

from knowledge_forge.models.source import SourceType


@dataclass
class RawItem:
    uri: str
    content: bytes
    content_hash: str
    modified_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    deleted: bool = False
    # Per-item tenancy override - lets a single collector instance that spans
    # several projects (e.g. one SlackCollector polling channels that belong
    # to different projects) tag each item correctly. None (the default)
    # falls back to the run-level project_id passed to run_pipeline().
    project_id: str | None = None


class Cursor(BaseModel):
    """Opaque incremental-sync bookmark; concrete collectors define their own shape."""

    data: dict[str, Any] = Field(default_factory=dict)


class Collector(ABC):
    source_type: ClassVar[SourceType]

    @abstractmethod
    def initial_sync(self) -> Iterator[RawItem]:
        """Yield every currently-visible item from the source."""

    @abstractmethod
    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        """Yield only items that changed (or were deleted) since `cursor`."""

    @abstractmethod
    def get_cursor(self) -> Cursor:
        """Return a cursor that captures the collector's current sync position."""

    def sync(self, cursor: Cursor | None = None) -> Iterator[RawItem]:
        return self.initial_sync() if cursor is None else self.incremental_sync(cursor)
