"""`search/document.py::read_document` - document expansion.

The fake-store tests cover the pagination contract without a database; the
`TestAgainstPostgres` class is the real-Postgres integration run (skipped
unless KF_TEST_DATABASE_URL is set, same convention as tests/storage/). The
DB tests seed rows under a test-only project_id/ID prefix and delete them
on teardown, since the dev database already holds real data.
"""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.search.document import read_document
from knowledge_forge.storage.store import PipelineStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

_PROJECT_ID = "test-read-document"


def _source(source_id: str = "test-rd-s1", modified_at: datetime | None = None) -> Source:
    return Source(
        source_id=source_id,
        source_type=SourceType.LOCAL_FILE,
        uri="file:///report.md",
        content_hash="h-rd",
        collected_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        modified_at=modified_at,
        project_id=_PROJECT_ID,
    )


def _document(document_id: str = "test-rd-d1", title: str | None = "2025 SH 업무 보고서") -> Document:
    return Document(
        document_id=document_id,
        source=SourceRef.from_source(_source()),
        elements=[TextBlock(element_id="e1", text="x")],
        metadata=DocumentMetadata(title=title),
    )


def _chunk(index: int, text: str, level: HierarchyLevel = HierarchyLevel.PARAGRAPH, document_id: str = "test-rd-d1") -> Chunk:
    return Chunk(
        chunk_id=f"test-rd-c{index}",
        document_id=document_id,
        source_id="test-rd-s1",
        hierarchy_level=level,
        sequence_index=index,
        text=text,
        token_count=len(text),
        element_ids=[f"e{index}"],
        page_range=(index + 1, index + 1),
    )


class _FakeStore:
    def __init__(self, document, chunks, source=None):
        self._document = document
        self._chunks = chunks
        self._source = source
        self.list_calls: list[tuple[str, HierarchyLevel | None]] = []

    async def get_document(self, document_id: str):
        return self._document if self._document is not None and self._document.document_id == document_id else None

    async def get_source(self, source_id: str):
        return self._source

    async def list_chunks_by_document(self, document_id: str, *, hierarchy_level=None):
        self.list_calls.append((document_id, hierarchy_level))
        # Deliberately handed back out of order - read_document must rely on
        # the store's ordering contract, not re-sort, so this fake mirrors
        # what the real store returns (ordered) via sorted().
        return sorted(self._chunks, key=lambda c: c.sequence_index)


async def test_read_document_returns_every_chunk_in_sequence_order_with_metadata():
    modified = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    store = _FakeStore(_document(), [_chunk(2, "셋"), _chunk(0, "하나"), _chunk(1, "둘")], source=_source(modified_at=modified))

    result = await read_document("test-rd-d1", store)

    assert result is not None
    assert result["document_id"] == "test-rd-d1"
    assert result["title"] == "2025 SH 업무 보고서"
    assert result["source"] == {
        "source_id": "test-rd-s1",
        "source_type": "local_file",
        "uri": "file:///report.md",
        "modified_at": modified.isoformat(),
    }
    assert result["total_chunks"] == 3
    assert result["total_tokens"] == 2 + 1 + 1
    assert result["offset"] == 0
    assert [c["sequence_index"] for c in result["chunks"]] == [0, 1, 2]
    assert [c["text"] for c in result["chunks"]] == ["하나", "둘", "셋"]
    assert result["chunks"][0] == {"chunk_id": "test-rd-c0", "sequence_index": 0, "page_start": 1, "page_end": 1, "text": "하나"}
    assert result["truncated"] is False
    assert result["next_offset"] is None
    # Every hierarchy level together - see PostgresStore.list_chunks_by_document.
    assert store.list_calls == [("test-rd-d1", None)]


async def test_read_document_returns_none_for_unknown_document():
    store = _FakeStore(_document(), [_chunk(0, "a")])

    assert await read_document("test-rd-nope", store) is None
    assert store.list_calls == []


async def test_read_document_title_is_none_when_metadata_has_none_and_source_row_is_missing():
    store = _FakeStore(_document(title=None), [_chunk(0, "a")], source=None)

    result = await read_document("test-rd-d1", store)

    assert result is not None
    assert result["title"] is None
    assert result["source"]["modified_at"] is None


async def test_read_document_max_chars_truncates_and_next_offset_continues_to_the_end():
    chunks = [_chunk(i, "가" * 10) for i in range(5)]
    store = _FakeStore(_document(), chunks)

    first = await read_document("test-rd-d1", store, max_chars=25)
    assert first is not None
    assert [c["sequence_index"] for c in first["chunks"]] == [0, 1]
    assert first["truncated"] is True
    assert first["next_offset"] == 2
    assert first["total_chunks"] == 5

    second = await read_document("test-rd-d1", store, max_chars=25, offset=first["next_offset"])
    assert second is not None
    assert second["offset"] == 2
    assert [c["sequence_index"] for c in second["chunks"]] == [2, 3]
    assert second["truncated"] is True
    assert second["next_offset"] == 4

    third = await read_document("test-rd-d1", store, max_chars=25, offset=second["next_offset"])
    assert third is not None
    assert [c["sequence_index"] for c in third["chunks"]] == [4]
    assert third["truncated"] is False
    assert third["next_offset"] is None


async def test_read_document_always_includes_the_first_chunk_of_a_page_even_over_budget():
    # A single chunk longer than max_chars must still make progress - an
    # empty page with the same next_offset would loop a caller forever.
    store = _FakeStore(_document(), [_chunk(0, "가" * 100), _chunk(1, "나")])

    first = await read_document("test-rd-d1", store, max_chars=10)
    assert first is not None
    assert [c["sequence_index"] for c in first["chunks"]] == [0]
    assert first["next_offset"] == 1

    second = await read_document("test-rd-d1", store, max_chars=10, offset=1)
    assert second is not None
    assert [c["sequence_index"] for c in second["chunks"]] == [1]
    assert second["truncated"] is False


async def test_read_document_offset_past_the_end_returns_an_empty_page_not_an_error():
    store = _FakeStore(_document(), [_chunk(0, "a"), _chunk(1, "b")])

    result = await read_document("test-rd-d1", store, offset=7)

    assert result is not None
    assert result["chunks"] == []
    assert result["truncated"] is False
    assert result["next_offset"] is None
    assert result["total_chunks"] == 2


async def test_read_document_negative_offset_is_treated_as_zero():
    store = _FakeStore(_document(), [_chunk(0, "a")])

    result = await read_document("test-rd-d1", store, offset=-3)

    assert result is not None
    assert result["offset"] == 0
    assert [c["sequence_index"] for c in result["chunks"]] == [0]


async def test_read_document_rejects_non_positive_max_chars():
    store = _FakeStore(_document(), [_chunk(0, "a")])

    with pytest.raises(ValueError):
        await read_document("test-rd-d1", store, max_chars=0)


@pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")
class TestAgainstPostgres:
    @pytest_asyncio.fixture
    async def store(self, tmp_path):
        async with PipelineStore(DSN, tmp_path) as ps:
            await ps.init_schema()
            db = ps._db
            await db.save_source(_source(modified_at=datetime(2026, 8, 30, tzinfo=timezone.utc)), None)
            await db.save_document(_document())
            # Saved out of order and with mixed hierarchy levels on purpose:
            # the store must order by sequence_index and read_document must
            # not drop the sentence-level chunk.
            await db.save_chunks(
                [
                    _chunk(2, "세 번째 문단 " * 20, HierarchyLevel.SENTENCE),
                    _chunk(0, "첫 번째 문단 " * 20),
                    _chunk(1, "두 번째 문단 " * 20),
                ]
            )
            yield ps
            await db.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'test-rd-%'")
            await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-rd-%'")
            await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-rd-%'")

    async def test_reads_whole_document_in_order(self, store):
        result = await read_document("test-rd-d1", store)

        assert result is not None
        assert result["title"] == "2025 SH 업무 보고서"
        assert result["source"]["source_id"] == "test-rd-s1"
        assert result["source"]["modified_at"] == "2026-08-30T00:00:00+00:00"
        assert [c["sequence_index"] for c in result["chunks"]] == [0, 1, 2]
        assert result["chunks"][0]["text"].startswith("첫 번째")
        assert result["chunks"][2]["text"].startswith("세 번째")
        assert result["total_chunks"] == 3
        assert result["total_tokens"] == sum(len("세 번째 문단 " * 20) for _ in range(3))
        assert result["truncated"] is False

    async def test_paginates_with_next_offset_until_exhausted(self, store):
        seen: list[int] = []
        offset = 0
        for _ in range(10):
            page = await read_document("test-rd-d1", store, max_chars=len("첫 번째 문단 " * 20) + 5, offset=offset)
            assert page is not None
            seen.extend(c["sequence_index"] for c in page["chunks"])
            if not page["truncated"]:
                break
            offset = page["next_offset"]
        assert seen == [0, 1, 2]

    async def test_unknown_document_returns_none(self, store):
        assert await read_document("test-rd-does-not-exist", store) is None
