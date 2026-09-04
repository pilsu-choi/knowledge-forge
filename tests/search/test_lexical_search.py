"""`document_search`/`lexical_search` (search/service.py) are thin wrappers
over the store - these check the wrapper contract (blank-query short
circuit, argument forwarding, `target_kind`/`document_id` always present)
against a fake store; the SQL itself is covered by
tests/storage/test_lexical_store.py."""

from datetime import datetime, timezone

from knowledge_forge.search.service import document_search, lexical_search


class _FakeStore:
    def __init__(self, hits):
        self._hits = hits
        self.title_calls: list[tuple] = []
        self.chunk_calls: list[tuple] = []

    async def search_documents_by_title(self, query, *, project_id=None, source_type=None, since=None, until=None, limit=10):
        self.title_calls.append((query, project_id, source_type, since, until, limit))
        return [dict(h) for h in self._hits]

    async def search_chunks_lexical(self, query, *, project_id=None, since=None, until=None, limit=10):
        self.chunk_calls.append((query, project_id, since, until, limit))
        return [dict(h) for h in self._hits]


async def test_document_search_forwards_arguments_and_strips_the_query():
    store = _FakeStore([{"document_id": "d1", "title": "T", "score": 1.0}])
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    until = datetime(2026, 9, 1, tzinfo=timezone.utc)

    hits = await document_search("  SH 보고서 ", store, project_id="p", source_type="google_drive", limit=3, since=since, until=until)

    assert store.title_calls == [("SH 보고서", "p", "google_drive", since, until, 3)]
    assert hits == [{"document_id": "d1", "title": "T", "score": 1.0, "target_kind": "document"}]


async def test_document_search_keeps_the_store_target_kind_when_present():
    store = _FakeStore([{"document_id": "d1", "target_kind": "document"}])

    hits = await document_search("q", store)

    assert hits[0]["target_kind"] == "document"
    assert store.title_calls == [("q", None, None, None, None, 10)]


async def test_document_search_returns_empty_for_blank_query_without_touching_store():
    store = _FakeStore([{"document_id": "should-not-appear"}])

    assert await document_search("   ", store) == []
    assert store.title_calls == []


async def test_lexical_search_forwards_arguments_and_tags_chunk_hits():
    store = _FakeStore([{"document_id": "d1", "chunk_id": "c1", "summary": "…ERR…", "score": 1.0}])
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)

    hits = await lexical_search("ERR_CONNECTION_RESET", store, project_id="p", limit=5, since=since)

    assert store.chunk_calls == [("ERR_CONNECTION_RESET", "p", since, None, 5)]
    assert hits[0]["target_kind"] == "chunk"
    assert hits[0]["document_id"] == "d1"


async def test_lexical_search_returns_empty_for_blank_query():
    store = _FakeStore([{"document_id": "x"}])

    assert await lexical_search("", store) == []
    assert store.chunk_calls == []
