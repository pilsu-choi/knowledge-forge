"""Derives `documents.title` (storage/schema.sql) - the one string
`search/service.py::document_search` matches a "2025_SH_업무_보고서 찾아줘"
style navigational query against.

Why this exists at all: `Document.metadata.title` is what a parser managed
to read out of the file itself, and for everything the SaaS collectors
bring in that is NULL - a Drive PDF's parser sees bytes, not the Drive file
name; a Gmail thread's parser sees a rendered body, not the subject line.
The name a *person* knows the document by lives in `sources.metadata`
(`name` for google_drive, `subject` for gmail, `channel` for slack), so the
title has to be assembled from both rows. Doing it once at write time (and
`PostgresStore.backfill_document_titles` for rows written before the column
existed) keeps the search query a plain indexed column match instead of a
CASE over two JSONB blobs.

Pure functions, no I/O - `PostgresStore.save_document` feeds it the model,
the backfill feeds it raw row values; both go through `derive_document_title`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

# Where a title lives in `sources.metadata`, per collector (see
# collectors/saas/*.py) - checked in order, first non-empty wins. `name` is
# also what a local_file/notion collector would most naturally use, so it
# is tried for every source type rather than keyed by type.
_SOURCE_TITLE_KEYS = ("title", "name", "subject", "file_name", "filename")

# Slack threads carry no human title at all (only a channel id and
# timestamps), so the derived title is "#<channel> <date>" - enough for the
# agent to tell threads apart in a candidate list and for a "#general 8월"
# style query to hit, without pretending a thread has a name. Prefers
# `channel_name` when a collector ever adds it, falling back to the id.
_SLACK_CHANNEL_KEYS = ("channel_name", "channel")

_MAX_TITLE_CHARS = 300


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    return text[:_MAX_TITLE_CHARS]


def first_heading_text(elements: Iterable[Any]) -> str | None:
    """The text of the first `heading` element, from either parsed
    `DocumentElement` models or their JSONB dict form (what a backfill reads
    back from `documents.elements`) - the last-resort title when neither the
    parser nor the source metadata had one."""
    for element in elements:
        if isinstance(element, dict):
            kind, text = element.get("type"), element.get("text")
        else:
            kind, text = getattr(element, "type", None), getattr(element, "text", None)
        if kind == "heading":
            return _clean(text)
    return None


def derive_document_title(
    *,
    metadata_title: str | None,
    source_type: str | None,
    source_metadata: dict[str, Any] | None,
    content_at: datetime | None,
    first_heading: str | None,
) -> str | None:
    """Priority: parser-supplied `metadata_title` -> source `name`/`subject`
    -> slack `#channel YYYY-MM-DD` -> first heading -> None.

    The parser title goes first because when it exists it was authored into
    the document (a DOCX core-properties title, an HTML <title>) and is more
    specific than a file name; the source name/subject comes next because
    it is the name the user actually typed into a "find X" request; the
    heading is last because a heading is a guess at a title, not a title
    (the Slack/Gmail context parsers emit synthetic headings like
    "Slack #C0BG… 스레드" that only the pipeline ever chose)."""
    title = _clean(metadata_title)
    if title:
        return title

    metadata = source_metadata or {}
    for key in _SOURCE_TITLE_KEYS:
        title = _clean(metadata.get(key))
        if title:
            return title

    if source_type == "slack":
        channel = None
        for key in _SLACK_CHANNEL_KEYS:
            channel = _clean(metadata.get(key))
            if channel:
                break
        if channel:
            date_part = f" {content_at.date().isoformat()}" if content_at is not None else ""
            return f"#{channel.lstrip('#')}{date_part}"

    return _clean(first_heading)
