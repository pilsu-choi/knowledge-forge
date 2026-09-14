"""Collector interface.

Generic enough to cover both filesystem walks (File sources) and paginated
API polling (SaaS sources): `RawItem.deleted` expresses delete-detection
uniformly, `content_hash` expresses change-detection uniformly, and
`Cursor` is an opaque per-collector bookmark (a manifest checkpoint for
files, a page token / updated-after timestamp for SaaS APIs).
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from knowledge_forge.models.source import SourceType


def ensure_utc(value: datetime) -> datetime:
    """Normalizes a naive datetime (no tzinfo - e.g. from cli.py's `--since`/
    `--until` or a request body's ISO string with no offset) to UTC, and
    passes an already-aware datetime through unchanged. Every `RawItem.
    modified_at` a Collector produces is UTC-aware, so `backfill_sync`'s
    `since`/`until` comparisons need the same to compare correctly - callers
    that accept `since`/`until` from a human (cli.py's `backfill` command,
    api/routes/ingest.py's IngestRequest) should normalize through this
    before passing them on.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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
    # True for a collector whose items are already the semantic units a
    # ContextReconstructor would otherwise build (episodes/threads already
    # merged) - see collectors/replay.py. pipeline.py reads this to decide
    # whether to run `default_reconstructor_for(source_type)` at all; a
    # reconstructor built for raw per-message/per-email items would either
    # crash (SlackConversationReconstructor needs `metadata["ts"]`, which a
    # merged episode doesn't carry) or silently re-merge and change the
    # content it hashes (GmailThreadReconstructor re-wraps the text with a
    # "From: ..." header), producing a different document_id for bytes that
    # were never supposed to change on replay.
    already_reconstructed: ClassVar[bool] = False

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

    def backfill_sync(self, since: datetime, until: datetime | None = None) -> Iterator[RawItem]:
        """Yield only items modified in `[since, until]`, independent of (and
        without touching) the collector's incremental-sync cursor - for
        recovering a window of data missed while scheduled sync wasn't
        running (see pipeline.py's `since`/`until` and cli.py's `backfill`
        command).

        Default implementation: re-runs initial_sync() and filters by
        `RawItem.modified_at`. Correct for every collector, but re-fetches
        everything to do it - collectors with a windowed API (SlackCollector,
        GmailCollector) override this with a pushed-down time-bounded query
        instead.
        """
        for item in self.initial_sync():
            if item.modified_at >= since and (until is None or item.modified_at <= until):
                yield item
