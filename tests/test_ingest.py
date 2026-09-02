"""Unit test for run_local_ingest (src/knowledge_forge/ingest.py), the
worker's pipeline-invocation helper - store=None here since only the
returned summary counts matter, not persistence (that's covered by
tests/storage/test_job_store.py and the worker's own live-DB test).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas import factory
from knowledge_forge.collectors.saas.base import SaaSCollector
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.ingest import run_local_ingest, run_saas_ingest
from knowledge_forge.models.source import SourceType


class _FakeMessages:
    def __init__(self):
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            parsed_output=ExtractionResult(
                summary="stub summary",
                facts=[],
                entities=[],
                relations=[],
                topics=[],
                document_type="text",
            )
        )


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


class _FakeSaaSCollector(SaaSCollector):
    source_type = SourceType.SLACK

    def __init__(self, client):
        super().__init__(client=client)
        # Populated for cursor-persistence assertions - not exercised by
        # tests that don't care about it.
        self.sync_calls: list[Cursor | None] = []
        self.cursor_to_return = Cursor(data={"channels": {"c": "2.0"}})

    def initial_sync(self):
        yield RawItem(
            uri="slack://c/1",
            content=b"hello from slack",
            content_hash="h1",
            modified_at=datetime.now(timezone.utc) - timedelta(days=30),
            metadata={"channel": "c", "ts": "1.0"},
        )
        yield RawItem(
            uri="slack://c/2",
            content=b"more recent slack message",
            content_hash="h2",
            modified_at=datetime.now(timezone.utc),
            metadata={"channel": "c", "ts": "2.0"},
        )

    def incremental_sync(self, cursor):
        return iter(())

    def sync(self, cursor=None):
        self.sync_calls.append(cursor)
        return super().sync(cursor)

    def get_cursor(self):
        return self.cursor_to_return


class _FakeCursorStore:
    """Minimal stand-in for PipelineStore covering exactly what run_pipeline
    (via store is not None) and run_saas_ingest's cursor load/save need -
    same minimal-fake philosophy as test_pipeline_dedup_and_supersedes.py's
    _FakeStore."""

    def __init__(self, initial_cursor_data=None):
        self._cursor_data = initial_cursor_data
        self.saved_cursor_data = None

    async def record_source(self, source, item):
        pass

    async def record_document(self, document):
        pass

    async def record_chunks(self, chunks):
        pass

    async def record_knowledge_objects(self, objects):
        pass

    async def record_relations(self, relations):
        pass

    async def list_prior_knowledge_objects(self, source_id):
        return []

    async def get_sync_cursor(self, source):
        return self._cursor_data

    async def save_sync_cursor(self, source, cursor_data):
        self.saved_cursor_data = cursor_data


async def test_run_saas_ingest_builds_collector_from_source_and_returns_counts(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )
    monkeypatch.setitem(factory.SAAS_COLLECTOR_BUILDERS, "slack", lambda: _FakeSaaSCollector(client=object()))

    result = await run_saas_ingest("slack", max_tokens=4000, model=None, store=None)

    assert result["object_count"] >= 1
    assert "relation_count" in result


async def test_run_saas_ingest_with_since_only_collects_the_backfill_window(monkeypatch):
    """`since`/`until` (from api/routes/ingest.py's IngestRequest, threaded through
    worker.py's _dispatch) route the fake collector through Collector.backfill_sync's
    default (list-everything-then-filter) - only the recent of the two RawItems
    _FakeSaaSCollector yields falls in [since, until]."""
    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )
    monkeypatch.setitem(factory.SAAS_COLLECTOR_BUILDERS, "slack", lambda: _FakeSaaSCollector(client=object()))

    result = await run_saas_ingest(
        "slack",
        max_tokens=4000,
        model=None,
        store=None,
        since=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    assert result["object_count"] >= 1
    assert client.messages.calls == 1  # only the in-window item reached extraction


async def test_run_saas_ingest_persists_the_collectors_cursor_after_a_successful_run(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )
    collector = _FakeSaaSCollector(client=object())
    monkeypatch.setitem(factory.SAAS_COLLECTOR_BUILDERS, "slack", lambda: collector)
    store = _FakeCursorStore(initial_cursor_data=None)

    await run_saas_ingest("slack", max_tokens=4000, model=None, store=store)

    # No prior cursor -> Collector.sync(None) -> initial_sync (base.py).
    assert collector.sync_calls == [None]
    assert store.saved_cursor_data == {"channels": {"c": "2.0"}}


async def test_run_saas_ingest_loads_the_persisted_cursor_and_syncs_incrementally(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )
    collector = _FakeSaaSCollector(client=object())
    monkeypatch.setitem(factory.SAAS_COLLECTOR_BUILDERS, "slack", lambda: collector)
    store = _FakeCursorStore(initial_cursor_data={"channels": {"c": "1.0"}})

    await run_saas_ingest("slack", max_tokens=4000, model=None, store=store)

    assert len(collector.sync_calls) == 1
    assert collector.sync_calls[0].data == {"channels": {"c": "1.0"}}


async def test_run_saas_ingest_backfill_does_not_touch_the_persisted_cursor(monkeypatch):
    """A --since/--until backfill re-collects a window independent of the
    incremental position (mirrors Collector.backfill_sync's own docstring) -
    it must neither read nor overwrite the persisted cursor."""
    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )
    collector = _FakeSaaSCollector(client=object())
    monkeypatch.setitem(factory.SAAS_COLLECTOR_BUILDERS, "slack", lambda: collector)
    store = _FakeCursorStore(initial_cursor_data={"channels": {"c": "1.0"}})

    await run_saas_ingest(
        "slack", max_tokens=4000, model=None, store=store, since=datetime.now(timezone.utc) - timedelta(hours=1)
    )

    assert collector.sync_calls == []  # backfill_sync was used instead of sync()
    assert store.saved_cursor_data is None  # untouched - still whatever it started as


async def test_run_local_ingest_returns_object_and_relation_counts(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("# A\n\nHello world.")
    (tmp_path / "b.txt").write_text("Just some plain text.")

    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )

    result = await run_local_ingest(tmp_path, max_tokens=4000, model=None, store=None)

    assert result["object_count"] >= 2  # at least one KO per file
    assert "relation_count" in result
    assert client.messages.calls >= 2


async def test_run_local_ingest_writes_okf_tree_and_manifest_once(tmp_path, monkeypatch):
    """A job is a one-shot run (unlike `schedule`'s repeating tick) - the
    manifest/views only need to be written once, at the end (see
    ingest.py's module docstring)."""
    from knowledge_forge.okf import OKFWriter

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.md").write_text("# A\n\nHello world.")
    okf_dir = tmp_path / "okf"

    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )
    okf_writer = OKFWriter(okf_dir, project_id="PRJ-X")

    result = await run_local_ingest(src_dir, max_tokens=4000, model=None, store=None, project_id="PRJ-X", okf_writer=okf_writer)

    assert result["object_count"] >= 1
    manifest_path = okf_dir / "PRJ-X" / "manifest.yaml"
    assert manifest_path.exists()
    resource_files = [p for p in (okf_dir / "PRJ-X" / "resources").glob("*.md") if p.name != "index.md"]
    assert len(resource_files) == 1
