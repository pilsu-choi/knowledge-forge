"""Integration tests for the document-title / lexical-search side of
`PostgresStore` (save_document title derivation, backfill_document_titles,
search_documents_by_title, search_chunks_lexical) against a real Postgres -
skipped unless KF_TEST_DATABASE_URL is set, like test_postgres_store.py.

Every row is written under `_PROJECT` and deleted on teardown, and every
search passes `project_id=_PROJECT`: the dev database this runs against
already holds hundreds of real documents, and an unscoped query would let
them crowd the test rows out of a `LIMIT`."""

import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, Heading, TextBlock
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.postgres_store import PostgresStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

_PROJECT = "test-lexical-proj"
_T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _source(idx: int, source_type: SourceType, metadata: dict, modified_at: datetime | None = _T0) -> Source:
    return Source(
        source_id=f"test-lex-s{idx}",
        source_type=source_type,
        uri=f"test://{idx}",
        content_hash=f"h{idx}",
        collected_at=_T0,
        modified_at=modified_at,
        metadata=metadata,
        project_id=_PROJECT,
    )


def _document(idx: int, src: Source, *, title: str | None = None, heading: str | None = None, body: str = "본문") -> Document:
    elements = [TextBlock(element_id=f"e{idx}-0", text=body)]
    if heading:
        elements.insert(0, Heading(element_id=f"e{idx}-h", level=1, text=heading))
    return Document(
        document_id=f"test-lex-d{idx}",
        source=SourceRef.from_source(src),
        elements=elements,
        metadata=DocumentMetadata(title=title),
    )


def _chunk(idx: int, doc: Document, text: str, seq: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"test-lex-c{idx}-{seq}",
        document_id=doc.document_id,
        source_id=doc.source.source_id,
        hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=seq,
        text=text,
        token_count=len(text.split()),
        element_ids=[],
    )


@pytest_asyncio.fixture
async def store():
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        yield db
        await db.conn.execute("DELETE FROM chunks WHERE source_id IN (SELECT source_id FROM sources WHERE project_id = %s)", (_PROJECT,))
        await db.conn.execute("DELETE FROM documents WHERE project_id = %s", (_PROJECT,))
        await db.conn.execute("DELETE FROM sources WHERE project_id = %s", (_PROJECT,))


async def _seed(store: PostgresStore) -> dict[str, Document]:
    """Four documents that between them cover every title-derivation branch,
    with distinct modified_at values for the time-filter test."""
    s1 = _source(1, SourceType.GOOGLE_DRIVE, {"name": "2025_SH_업무_보고서.pdf", "file_id": "f1"}, _T0)
    s2 = _source(2, SourceType.GOOGLE_DRIVE, {"name": "2024_SH_업무_보고서_v2.xlsx", "file_id": "f2"}, _T0 + timedelta(days=10))
    s3 = _source(3, SourceType.GMAIL, {"subject": "[NH농협] 고객사 산출물 기간 변경"}, _T0 + timedelta(days=20))
    s4 = _source(4, SourceType.SLACK, {"channel": "C0BGQEQG1L6", "thread_ts": "1"}, _T0 + timedelta(days=30))
    docs = {
        "d1": _document(1, s1, heading="무시될 제목"),
        "d2": _document(2, s2),
        "d3": _document(3, s3, heading="Gmail Thread: 산출물"),
        "d4": _document(4, s4, heading="Slack #C0BGQEQG1L6 스레드"),
    }
    for src in (s1, s2, s3, s4):
        await store.save_source(src, raw_path=None)
    for doc in docs.values():
        await store.save_document(doc)
    return docs


async def _titles(store: PostgresStore) -> dict[str, str | None]:
    rows = await (await store.conn.execute("SELECT document_id, title FROM documents WHERE project_id = %s", (_PROJECT,))).fetchall()
    return dict(rows)


async def test_save_document_derives_title_from_source_metadata_per_source_type(store):
    await _seed(store)

    titles = await _titles(store)

    assert titles["test-lex-d1"] == "2025_SH_업무_보고서.pdf"  # drive name beats the heading
    assert titles["test-lex-d3"] == "[NH농협] 고객사 산출물 기간 변경"  # gmail subject
    assert titles["test-lex-d4"] == "#C0BGQEQG1L6 2026-08-31"  # slack channel + content date


async def test_save_document_prefers_parser_title_and_falls_back_to_heading(store):
    src = _source(5, SourceType.LOCAL_FILE, {})
    await store.save_source(src, raw_path=None)

    await store.save_document(_document(5, src, title="파서가 읽은 제목", heading="첫 제목"))
    await store.save_document(_document(6, src, heading="첫 제목만"))
    await store.save_document(_document(7, src))

    titles = await _titles(store)
    assert titles["test-lex-d5"] == "파서가 읽은 제목"
    assert titles["test-lex-d6"] == "첫 제목만"
    assert titles["test-lex-d7"] is None


async def test_backfill_fills_only_null_titles_unless_overwrite(store):
    await _seed(store)
    await store.conn.execute("UPDATE documents SET title = NULL WHERE project_id = %s", (_PROJECT,))
    await store.conn.execute("UPDATE documents SET title = 'manual' WHERE document_id = 'test-lex-d2'")

    updated = await store.backfill_document_titles(_PROJECT)
    titles = await _titles(store)

    assert updated == 3
    assert titles["test-lex-d1"] == "2025_SH_업무_보고서.pdf"
    assert titles["test-lex-d2"] == "manual"
    assert titles["test-lex-d4"] == "#C0BGQEQG1L6 2026-08-31"

    assert await store.backfill_document_titles(_PROJECT) == 0  # nothing left to fill
    assert await store.backfill_document_titles(_PROJECT, overwrite=True) == 4
    assert (await _titles(store))["test-lex-d2"] == "2024_SH_업무_보고서_v2.xlsx"


async def test_search_documents_by_title_matches_korean_substring(store):
    await _seed(store)

    hits = await store.search_documents_by_title("산출물 기간", project_id=_PROJECT)

    assert [h["document_id"] for h in hits] == ["test-lex-d3"]
    hit = hits[0]
    assert hit["target_kind"] == "document"
    assert hit["title"] == "[NH농협] 고객사 산출물 기간 변경"
    assert hit["source_type"] == "gmail"
    assert hit["uri"] == "test://3"
    assert hit["score"] == 1.0
    assert hit["modified_at"] == (_T0 + timedelta(days=20)).isoformat()


async def test_search_documents_by_title_matches_a_file_name_with_extension(store):
    await _seed(store)

    hits = await store.search_documents_by_title("2025_SH_업무_보고서.pdf", project_id=_PROJECT)

    assert hits[0]["document_id"] == "test-lex-d1" and hits[0]["score"] == 1.0
    assert all(h["score"] < 1.0 for h in hits[1:])  # the near-identical 2024 sibling may follow as a fuzzy hit


async def test_search_documents_by_title_underscore_is_not_a_wildcard(store):
    src = _source(8, SourceType.GOOGLE_DRIVE, {"name": "2025aSHb업무"})
    await store.save_source(src, raw_path=None)
    await store.save_document(_document(8, src))

    hits = await store.search_documents_by_title("2025_SH_업무", project_id=_PROJECT)

    assert all(h["score"] < 1.0 for h in hits)  # at most a fuzzy hit, never a substring one


async def test_search_documents_by_title_returns_every_candidate_ranked(store):
    """"SH 보고서" is ambiguous - both SH reports must come back so the agent
    can ask which one; the exact-name query ranks its own document first."""
    await _seed(store)

    ambiguous = await store.search_documents_by_title("SH 보고서", project_id=_PROJECT)
    exact = await store.search_documents_by_title("2025_SH_업무_보고서", project_id=_PROJECT)

    assert {h["document_id"] for h in ambiguous} == {"test-lex-d1", "test-lex-d2"}
    assert all(0.5 < h["score"] < 1.0 for h in ambiguous)  # token tier, not substring
    assert exact[0]["document_id"] == "test-lex-d1" and exact[0]["score"] == 1.0
    assert [h["document_id"] for h in exact[1:]] == ["test-lex-d2"]  # fuzzy sibling ranks below


async def test_search_documents_by_title_filters_by_time_source_type_and_project(store):
    await _seed(store)

    since = _T0 + timedelta(days=5)
    until = _T0 + timedelta(days=25)
    in_window = await store.search_documents_by_title("SH", project_id=_PROJECT, since=since, until=until)
    by_type = await store.search_documents_by_title("업무", project_id=_PROJECT, source_type="gmail")
    other_project = await store.search_documents_by_title("SH", project_id="test-lexical-other")

    assert [h["document_id"] for h in in_window] == ["test-lex-d2"]
    assert by_type == []
    assert other_project == []


async def test_search_documents_by_title_ignores_untitled_rows(store):
    src = _source(9, SourceType.LOCAL_FILE, {})
    await store.save_source(src, raw_path=None)
    await store.save_document(_document(9, src))

    assert await store.search_documents_by_title("본문", project_id=_PROJECT) == []


async def test_search_chunks_lexical_returns_vector_search_shaped_hits(store):
    docs = await _seed(store)
    filler = "이 문장은 자리를 채우기 위한 것이다. " * 40
    await store.save_chunks(
        [
            _chunk(1, docs["d1"], "서두입니다. " + filler + "연결 오류 ERR_CONNECTION_RESET 가 발생했다. " + filler, seq=0),
            _chunk(1, docs["d1"], "아무 관련 없는 두 번째 청크", seq=1),
            _chunk(3, docs["d3"], "산출물 기간을 변경합니다 (err_connection_reset 참고)", seq=0),
        ]
    )

    hits = await store.search_chunks_lexical("ERR_CONNECTION_RESET", project_id=_PROJECT)

    assert [(h["document_id"], h["chunk_id"]) for h in hits] == [
        ("test-lex-d1", "test-lex-c1-0"),
        ("test-lex-d3", "test-lex-c3-0"),
    ]
    first = hits[0]
    assert first["target_kind"] == "chunk"
    assert first["knowledge_object_id"] is None
    assert first["type"] == "chunk"
    assert first["score"] == 1.0
    assert first["title"] == "2025_SH_업무_보고서.pdf"
    assert first["source_type"] == "google_drive"
    assert first["hierarchy_level"] == "paragraph"
    assert first["sequence_index"] == 0
    assert "ERR_CONNECTION_RESET" in first["summary"]
    assert len(first["summary"]) <= 502  # _CHUNK_HIT_SUMMARY_CHARS plus the two ellipses
    assert first["summary"].startswith("…")  # centered on the match, not the head of the chunk
    assert set(first) >= {"document_id", "chunk_id", "summary", "score", "project_id", "content_at", "modified_at", "uri"}


async def test_search_chunks_lexical_respects_time_window(store):
    docs = await _seed(store)
    await store.save_chunks([_chunk(1, docs["d1"], "티켓 KF-1234 처리"), _chunk(3, docs["d3"], "티켓 KF-1234 재처리")])

    hits = await store.search_chunks_lexical("KF-1234", project_id=_PROJECT, since=_T0 + timedelta(days=15))

    assert [h["document_id"] for h in hits] == ["test-lex-d3"]
