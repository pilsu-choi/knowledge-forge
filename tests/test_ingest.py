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

    def get_cursor(self):
        return Cursor()


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
    assert len(list((okf_dir / "PRJ-X" / "resources").glob("*.yaml"))) == 1
