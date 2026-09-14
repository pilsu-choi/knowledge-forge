"""Replays already-collected raw bytes back through the pipeline.

Every run persists the bytes it collected content-addressed on disk
(storage/raw_store.py) and records where they landed in `sources.raw_path`,
but until this module nothing could read them back: the only way to
re-parse a source whose parser was missing or broken *at collection time*
was to re-collect it from the origin API - re-authenticating, re-downloading,
and picking up whatever the remote looks like today instead of what was
actually collected. That is the gap this collector closes.

The concrete backlog it was written for: 235 of 246 `google_drive` sources
were collected before parsers/registry.py learned to fall back to
`RawItem.metadata["name"]` for the file extension (commit b6c9435 - a
Drive `webViewLink` carries no extension, so every one of them raised
UnsupportedFormatError and pipeline.py logged-and-skipped it). They each
got a `sources` row and a raw blob, but never a `documents` row. The bytes
were on disk the whole time; nothing could feed them back in.

`_make_source` recomputes `source_id` from `(collector.source_type, uri)`,
so a replay rewrites exactly the same `sources` rows instead of creating
duplicates - which is why `source_type` is an instance attribute here (it
adopts the type of the rows being replayed) rather than the fixed class
constant every origin collector uses.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from knowledge_forge.collectors.base import Collector, Cursor, RawItem
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType
from knowledge_forge.parsers.registry import extension_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayRow:
    """One `sources` row, reduced to what rebuilding its RawItem needs."""

    uri: str
    raw_path: str
    modified_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    project_id: str = "default"

    @property
    def extension(self) -> str:
        """The extension the parser registry would dispatch this row on."""
        return extension_for(self.uri, self.metadata.get("name"))


class RawBlobReplayCollector(Collector):
    # Instance-level override of Collector's `source_type` ClassVar: a
    # replay has no source type of its own, it adopts the one its rows were
    # collected under so the recomputed source_id matches. mypy rejects
    # narrowing a ClassVar to an instance attribute, but nothing writes
    # `Collector.source_type` as class state - every use is a read off an
    # instance.
    source_type: SourceType  # type: ignore[misc]
    # Every row this collector replays came from `sources`, which only ever
    # holds already-reconstructed items for slack/gmail (run_pipeline runs
    # the ContextReconstructor *before* `_make_source`/`record_source`, so a
    # raw per-message/per-email item never becomes a `sources` row on its
    # own - see context/slack.py and context/gmail.py). Re-running that same
    # reconstructor over a replayed episode/thread is not idempotent: Slack's
    # needs a per-message `ts` the merged metadata doesn't carry (KeyError),
    # and Gmail's re-wraps the already-merged text with a new "From: ..."
    # header, changing content_hash - and therefore document_id - out from
    # under an unchanged source. pipeline.py checks this flag to skip
    # reconstruction entirely for replay, so a replayed document's uri and
    # content_hash (and hence document_id) match the original run's exactly.
    already_reconstructed = True

    def __init__(self, rows: list[ReplayRow], source_type: SourceType):
        self.source_type = source_type
        self._rows = rows

    def initial_sync(self) -> Iterator[RawItem]:
        for row in self._rows:
            try:
                content = Path(row.raw_path).read_bytes()
            except OSError as exc:
                # A blob that is gone (pruned data dir, moved KF_DATA_DIR)
                # cannot be replayed and there is nothing to fall back to -
                # skip it the way pipeline.py skips an unparseable item,
                # rather than aborting a batch of hundreds over one file.
                logger.warning("raw blob unreadable for %s, skipping: %s", row.uri, exc)
                continue
            yield RawItem(
                uri=row.uri,
                content=content,
                # Recomputed rather than carried over from the row: the blob
                # on disk is the ground truth for what gets parsed, so the
                # rewritten `sources.content_hash` should describe it.
                content_hash=compute_content_hash(content),
                modified_at=row.modified_at,
                metadata=row.metadata,
                deleted=False,
                project_id=row.project_id,
            )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        """A replay set is already an explicit selection of rows, so there is
        no incremental position to advance - whatever the caller selected is
        what gets replayed, cursor or not."""
        return self.initial_sync()

    def get_cursor(self) -> Cursor:
        """Deliberately empty: a replay must never overwrite the origin
        collector's real sync cursor (`sync_cursors`), or the next scheduled
        Drive/Slack sync would resume from a position this run never reached.
        Callers persist nothing from here."""
        return Cursor(data={})


def filter_rows_by_extension(rows: list[ReplayRow], extensions: set[str]) -> list[ReplayRow]:
    """Narrows a replay batch to the given extensions (lower-cased, dot
    included). Used to keep an expensive or currently-unrunnable format out
    of a batch - e.g. replaying the text formats while the VLM-backed ones
    wait on credentials - without hand-listing individual rows."""
    return [row for row in rows if row.extension in extensions]
