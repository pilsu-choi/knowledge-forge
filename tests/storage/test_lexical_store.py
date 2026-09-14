"""Integration tests for the document-title / lexical-search side of
`PostgresStore` (save_document title derivation, backfill_document_titles,
search_documents_by_title, search_chunks_lexical) against a real Postgres -
skipped unless KF_TEST_DATABASE_URL is set, like test_postgres_store.py.

Every row is written under `_PROJECT` and deleted on teardown, and every
search passes `project_id=_PROJECT`: the dev database this runs against
already holds hundreds of real documents, and an unscoped query would let
them crowd the test rows out of a `LIMIT`."""

import os
import unicodedata
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, Heading, TextBlock
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.text_normalize import to_nfc

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
    """NFC-normalizes `text` here, the way `chunking/adaptive.py::AdaptiveChunker.chunk`
    does before a real Chunk ever reaches `save_chunks` (fix/etl-quick moved
    normalization there - `save_chunks` itself no longer normalizes, see its
    docstring) - so a test that hands `_chunk()` NFD text is exercising the
    same invariant a production chunk arrives with."""
    return Chunk(
        chunk_id=f"test-lex-c{idx}-{seq}",
        document_id=doc.document_id,
        source_id=doc.source.source_id,
        hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=seq,
        text=to_nfc(text),
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


async def test_search_documents_by_title_matches_tokens_out_of_order(store):
    """토큰이 전부 들어 있으면 순서와 구분자가 달라도 substring과 같은 1.0 - 실측된 회귀
    (v0-navigational-004, 질의 "Dev Handover README" vs 제목
    "README_ProjectIntel_Dev_Handover.md")를 그대로 옮긴 것이다. 판정 규칙 전부는
    `storage/title_match.py` 참고."""
    src = _source(10, SourceType.GOOGLE_DRIVE, {"name": "README_ProjectIntel_Dev_Handover.md"})
    await store.save_source(src, raw_path=None)
    await store.save_document(_document(10, src))

    hits = await store.search_documents_by_title("Dev Handover README", project_id=_PROJECT)

    assert hits[0]["document_id"] == "test-lex-d10" and hits[0]["score"] == 1.0


async def test_search_documents_by_title_gives_no_1_0_when_several_titles_contain_the_query_tokens(store):
    """토큰 집합 tier가 1.0을 양산하면 `_claim_anchor`가 엉뚱한 문서 하나로 검색을 좁힌다.
    질의 토큰을 통째로 담은 문서가 둘이면 어느 쪽도 "그 문서"가 아니므로 1.0을 주지 않는다."""
    await _seed(store)

    hits = await store.search_documents_by_title("보고서 SH", project_id=_PROJECT)

    assert {h["document_id"] for h in hits} == {"test-lex-d1", "test-lex-d2"}
    assert all(0.5 < h["score"] < 1.0 for h in hits)


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


# --- 유니코드 정규화 (NFC) -------------------------------------------------
# 버그: macOS/Google Drive 유래 파일명이 NFD(한글 자모 분해)로 들어와 `documents.title`/
# `chunks.text`에 그대로 저장되면, NFC로 친 질의가 substring tier(score 1.0)에 구조적으로
# 못 맞고 trigram tier로 떨어진다 - 정확히 일치하는 문서가 식별자가 다른 문서들보다 아래로
# 내려간다. 적재·질의 양쪽에 NFC를 걸어야 낫는다(text_normalize.py).

_NFC_NAME = "프로젝트 투입 관리_2026-031_한국자산관리공사.xlsx"
_NFD_NAME = unicodedata.normalize("NFD", _NFC_NAME)
_NFD_SUBJECT = f"이제 ‘{_NFD_NAME}’의 소유자가 되었습니다"  # 파일명만 NFD인 혼합 정규화


async def _nfd_document(store: PostgresStore) -> Document:
    """NFD 제목을 가진 gmail 소스 하나 + 이름이 비슷하지만 식별자가 다른 이웃 하나.
    이웃이 있어야 "trigram으로 떨어지면 엉뚱한 문서가 위로 온다"는 게 드러난다."""
    src = _source(20, SourceType.GMAIL, {"subject": _NFD_SUBJECT})
    other = _source(21, SourceType.GMAIL, {"subject": "이제 ‘프로젝트 투입 관리_2026-032_동양생명.xlsx’의 소유자가 되었습니다"})
    await store.save_source(src, raw_path=None)
    await store.save_source(other, raw_path=None)
    doc = _document(20, src)
    await store.save_document(doc)
    await store.save_document(_document(21, other))
    return doc


async def test_nfd_source_subject_is_stored_as_nfc(store):
    await _nfd_document(store)

    title = (await _titles(store))["test-lex-d20"]

    assert title == unicodedata.normalize("NFC", _NFD_SUBJECT)
    assert unicodedata.normalize("NFC", title) == title


async def test_search_documents_by_title_matches_an_nfd_title_with_an_nfc_query(store):
    await _nfd_document(store)

    hits = await store.search_documents_by_title(_NFC_NAME.removesuffix(".xlsx"), project_id=_PROJECT)

    assert hits[0]["document_id"] == "test-lex-d20"
    assert hits[0]["score"] == 1.0  # substring tier - 이웃이 위로 올라오지 않는다


async def test_search_documents_by_title_accepts_an_nfd_query(store):
    """사용자가 macOS/Drive에서 복사한 NFD 파일명을 그대로 붙여넣는 경우 - 저장값은 NFC라
    질의 쪽 정규화가 없으면 못 맞는다."""
    await _nfd_document(store)

    hits = await store.search_documents_by_title(_NFD_NAME.removesuffix(".xlsx"), project_id=_PROJECT)

    assert hits[0]["document_id"] == "test-lex-d20"
    assert hits[0]["score"] == 1.0


async def test_save_chunks_stores_nfc_and_matches_either_normalization(store):
    doc = await _nfd_document(store)
    await store.save_chunks([_chunk(20, doc, f"첨부 파일 {_NFD_NAME} 를 공유했습니다")])

    row = await (await store.conn.execute("SELECT text FROM chunks WHERE chunk_id = 'test-lex-c20-0'")).fetchone()
    by_nfc = await store.search_chunks_lexical(_NFC_NAME, project_id=_PROJECT)
    by_nfd = await store.search_chunks_lexical(_NFD_NAME, project_id=_PROJECT)

    assert unicodedata.normalize("NFC", row[0]) == row[0]
    assert [h["chunk_id"] for h in by_nfc] == ["test-lex-c20-0"] and by_nfc[0]["score"] == 1.0
    assert [h["chunk_id"] for h in by_nfd] == ["test-lex-c20-0"] and by_nfd[0]["score"] == 1.0


async def test_normalize_unicode_text_fixes_rows_written_before_the_fix(store):
    """마이그레이션(`db normalize-unicode`) - 적재 경로에 NFC가 걸리기 전에 쌓인 행.
    저장 경로가 이제 정규화하므로 NFD 행은 raw SQL로 직접 만든다."""
    doc = await _nfd_document(store)
    await store.save_chunks([_chunk(20, doc, f"첨부 파일 {_NFC_NAME} 를 공유했습니다")])
    await store.conn.execute("UPDATE documents SET title = %s WHERE document_id = 'test-lex-d20'", (_NFD_SUBJECT,))
    await store.conn.execute(
        "UPDATE chunks SET text = normalize(text, NFD) WHERE chunk_id = 'test-lex-c20-0'"
    )
    broken = await store.search_documents_by_title(_NFC_NAME, project_id=_PROJECT)
    assert all(h["score"] < 1.0 for h in broken)  # 버그 재현: substring tier가 죽고 trigram만 남는다

    documents, chunks = await store.normalize_unicode_text(_PROJECT)

    assert (documents, chunks) == (1, 1)
    assert (await _titles(store))["test-lex-d20"] == unicodedata.normalize("NFC", _NFD_SUBJECT)
    hits = await store.search_documents_by_title(_NFC_NAME, project_id=_PROJECT)
    assert hits[0]["document_id"] == "test-lex-d20" and hits[0]["score"] == 1.0
    assert [h["chunk_id"] for h in await store.search_chunks_lexical(_NFC_NAME, project_id=_PROJECT)] == ["test-lex-c20-0"]
    assert await store.normalize_unicode_text(_PROJECT) == (0, 0)  # 멱등
