"""Integration tests for `load_replay_rows` (storage/replay_index.py) - the
selection half of `knowledge-forge reparse`, which decides *which* already
collected sources get re-parsed. Skipped unless KF_TEST_DATABASE_URL is
set, like test_postgres_store.py.

Every row is written under `_PROJECT` and deleted on teardown, and every
call scopes to `project_id=_PROJECT`: the dev database this runs against
holds hundreds of real sources that would otherwise swamp the assertions.
"""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from knowledge_forge.models.document import Document, TextBlock
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.storage.replay_index import load_replay_rows

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

_PROJECT = "test-replay-proj"
_T0 = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _source(idx: int, source_type: SourceType = SourceType.GOOGLE_DRIVE, name: str = "a.md") -> Source:
    return Source(
        source_id=f"test-replay-s{idx}",
        source_type=source_type,
        uri=f"https://drive.google.com/file/d/replay{idx}/view",
        content_hash=f"h{idx}",
        collected_at=_T0,
        modified_at=_T0,
        metadata={"name": name},
        project_id=_PROJECT,
    )


def _document(idx: int, src: Source) -> Document:
    return Document(
        document_id=f"test-replay-d{idx}",
        source=SourceRef.from_source(src),
        elements=[TextBlock(element_id=f"test-replay-e{idx}", text="본문")],
    )


@pytest_asyncio.fixture
async def store():
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        yield db
        await db.conn.execute("DELETE FROM documents WHERE project_id = %s", (_PROJECT,))
        await db.conn.execute("DELETE FROM sources WHERE project_id = %s", (_PROJECT,))


async def _seed(store: PostgresStore) -> tuple[Source, Source, Source]:
    """s1 never produced a document (the recovery case), s2 did, s3 has no
    raw blob recorded at all (nothing to replay from)."""
    s1, s2, s3 = _source(1), _source(2), _source(3)
    await store.save_source(s1, raw_path="/tmp/replay1")
    await store.save_source(s2, raw_path="/tmp/replay2")
    await store.save_source(s3, raw_path=None)
    await store.save_document(_document(2, s2))
    return s1, s2, s3


async def test_only_missing_documents_selects_just_the_undocumented_source(store):
    s1, _, _ = await _seed(store)

    rows = await load_replay_rows(DSN, SourceType.GOOGLE_DRIVE, project_id=_PROJECT)

    assert [row.uri for row in rows] == [s1.uri]


async def test_all_selects_documented_sources_too_but_never_ones_without_a_blob(store):
    s1, s2, s3 = await _seed(store)

    rows = await load_replay_rows(DSN, SourceType.GOOGLE_DRIVE, project_id=_PROJECT, only_missing_documents=False)

    assert sorted(row.uri for row in rows) == sorted([s1.uri, s2.uri])
    assert s3.uri not in [row.uri for row in rows]


async def test_uris_narrows_to_exactly_those_sources(store):
    """The point of `uris`: re-parse one file that already has a document
    without dragging every sibling of its format through a re-parse."""
    _, s2, _ = await _seed(store)

    rows = await load_replay_rows(
        DSN, SourceType.GOOGLE_DRIVE, project_id=_PROJECT, only_missing_documents=False, uris=[s2.uri]
    )

    assert [row.uri for row in rows] == [s2.uri]


async def test_a_source_of_another_type_is_never_selected(store):
    await _seed(store)
    slack = _source(4, SourceType.SLACK)
    await store.save_source(slack, raw_path="/tmp/replay4")

    rows = await load_replay_rows(DSN, SourceType.GOOGLE_DRIVE, project_id=_PROJECT)

    assert slack.uri not in [row.uri for row in rows]


async def test_row_carries_what_rebuilding_the_raw_item_needs(store):
    s1, _, _ = await _seed(store)

    (row,) = await load_replay_rows(DSN, SourceType.GOOGLE_DRIVE, project_id=_PROJECT)

    assert row.raw_path == "/tmp/replay1"
    assert row.project_id == _PROJECT
    assert row.modified_at == _T0
    # metadata["name"] is the parser registry's extension fallback for a
    # Drive uri that carries none.
    assert row.metadata["name"] == s1.metadata["name"]
    assert row.extension == ".md"


async def test_exclude_uris_holds_a_source_out_of_the_batch(store):
    """2단계-A의 선택 방법: 한 포맷을 통째로 고르되 따로 적재할 한 건만 뺀다."""
    s1, s2, _ = await _seed(store)

    rows = await load_replay_rows(
        DSN, SourceType.GOOGLE_DRIVE, project_id=_PROJECT, only_missing_documents=False, exclude_uris=[s2.uri]
    )

    assert [row.uri for row in rows] == [s1.uri]


async def test_exclude_uris_wins_over_uris_when_both_name_the_same_source(store):
    """Both filters apply; nothing is quietly re-included."""
    _, s2, _ = await _seed(store)

    rows = await load_replay_rows(
        DSN,
        SourceType.GOOGLE_DRIVE,
        project_id=_PROJECT,
        only_missing_documents=False,
        uris=[s2.uri],
        exclude_uris=[s2.uri],
    )

    assert rows == []
