"""Selects which already-collected sources to replay (collectors/replay.py).

Kept out of postgres_store.py on purpose: this is a one-off recovery query
against `sources`/`documents` used by a single CLI command, not part of the
pipeline's persistence facade, and it wants its own short-lived connection
so a replay selection can be made before (and independently of) the
PipelineStore the replay itself writes through.
"""

from typing import Any

import psycopg

from knowledge_forge.collectors.replay import ReplayRow
from knowledge_forge.models.source import SourceType

# `documents.source_id` is the only link back to `sources`, so "was this
# source ever turned into a document?" is a plain LEFT JOIN ... IS NULL -
# which is also what makes a replay resumable: re-running with
# `only_missing_documents=True` picks up exactly what the previous attempt
# failed to produce, with no bookkeeping of its own.
_SELECT = """
    SELECT s.uri, s.raw_path, COALESCE(s.modified_at, s.collected_at), s.metadata, s.project_id
    FROM sources s
    LEFT JOIN documents d ON d.source_id = s.source_id
    WHERE s.source_type = %(source_type)s
      AND (%(project_id)s::text IS NULL OR s.project_id = %(project_id)s)
      AND s.raw_path IS NOT NULL
      AND (%(uris)s::text[] IS NULL OR s.uri = ANY(%(uris)s))
      AND (NOT %(only_missing)s OR d.document_id IS NULL)
    GROUP BY s.source_id, s.uri, s.raw_path, s.modified_at, s.collected_at, s.metadata, s.project_id
    ORDER BY s.collected_at
"""


async def load_replay_rows(
    dsn: str,
    source_type: SourceType,
    project_id: str | None = None,
    only_missing_documents: bool = True,
    uris: list[str] | None = None,
    limit: int | None = None,
) -> list[ReplayRow]:
    """Every `sources` row of `source_type` that still has its raw blob
    recorded, oldest collection first. `only_missing_documents` (the
    default) restricts it to the rows that never produced a `documents`
    row - the recovery case; pass False to re-parse everything, e.g. after
    a parser's *output* (not its availability) changed.

    `uris` narrows the batch to exactly those `sources.uri` values. It
    exists so re-parsing one known-bad file does not require `--all` over
    its whole format: the ids are deterministic so a blanket re-parse would
    not duplicate rows, but it does rewrite every sibling document's
    `elements`/`updated_at`, and a re-parse that yields *fewer* chunks than
    before leaves the surplus behind (chunk writes are upserts, nothing
    deletes an orphan). Re-parsing 20 healthy workbooks to fix 1 is not
    worth either."""
    params: dict[str, Any] = {
        "source_type": source_type.value,
        "project_id": project_id,
        "only_missing": only_missing_documents,
        "uris": uris,
    }
    sql = _SELECT + ("\n    LIMIT %(limit)s" if limit is not None else "")
    if limit is not None:
        params["limit"] = limit

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()

    return [
        ReplayRow(
            uri=row[0],
            raw_path=row[1],
            modified_at=row[2],
            metadata=row[3] or {},
            project_id=row[4],
        )
        for row in rows
    ]
