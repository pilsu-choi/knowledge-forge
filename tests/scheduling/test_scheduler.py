"""Tests for the scheduler's per-tick logic (build_tick), not the actual
AsyncIOScheduler loop - run_scheduled_sync runs forever as a background job
on the event loop, so it isn't unit-testable; build_tick exists precisely
so the (async) tick body can be exercised directly.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.scheduling.scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    SYNC_JOB_ID,
    build_tick,
    build_trigger_app,
    run_scheduled_sync,
    trigger_now,
)

LOGGER_NAME = "knowledge_forge.scheduling.scheduler"


class _FakeMessages:
    def __init__(self, raise_error: bool = False):
        self._raise_error = raise_error
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        if self._raise_error:
            raise RuntimeError("simulated LLM outage")
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
    def __init__(self, raise_error: bool = False):
        self.messages = _FakeMessages(raise_error=raise_error)


def _make_tick(tmp_path, client, store_factory=None):
    collector = LocalFileCollector(root=tmp_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = ClaudeExtractor(client=client)
    return collector, build_tick(collector, registry, chunker, extractor, store_factory=store_factory)


def _produced_count(caplog) -> int:
    for record in caplog.records:
        msg = record.getMessage()
        if "produced" in msg and "Knowledge Object" in msg:
            # "scheduled sync tick produced %d Knowledge Object(s)"
            return int(msg.split("produced")[1].strip().split()[0])
    raise AssertionError(f"no 'produced N Knowledge Object(s)' log line found in {[r.getMessage() for r in caplog.records]}")


async def test_first_tick_runs_initial_sync_and_produces_knowledge_objects(tmp_path, caplog):
    (tmp_path / "a.md").write_text("# A\n\nHello world.")
    (tmp_path / "b.txt").write_text("Just some plain text.")

    client = _FakeClient()
    collector, tick = _make_tick(tmp_path, client)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await tick()

    assert _produced_count(caplog) >= 2  # at least one KO per file
    assert client.messages.calls >= 2


async def test_second_tick_only_picks_up_new_file_incrementally(tmp_path, caplog):
    (tmp_path / "a.md").write_text("# A\n\nHello world.")
    (tmp_path / "b.txt").write_text("Just some plain text.")

    client = _FakeClient()
    collector, tick = _make_tick(tmp_path, client)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await tick()
    first_calls = client.messages.calls

    (tmp_path / "c.txt").write_text("A brand new file added after the first tick.")

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await tick()

    second_tick_count = _produced_count(caplog)
    # Only the new file's chunk(s) should have been (re-)extracted this tick.
    assert second_tick_count >= 1
    assert client.messages.calls > first_calls
    assert client.messages.calls - first_calls == second_tick_count


async def test_tick_swallows_exceptions_and_logs_instead_of_raising(tmp_path, caplog):
    (tmp_path / "a.txt").write_text("Some text that will fail extraction.")

    client = _FakeClient(raise_error=False)
    # Force a failure that isn't caught as a normal KnowledgeExtractionError
    # by making chunk collection itself blow up - simplest reliable way: point
    # the collector at a path that raises when walked (nonexistent directory
    # component causes rglob to error is unreliable across platforms), so
    # instead we simulate a hard failure inside the pipeline by making the
    # fake Anthropic client's underlying call always error - ClaudeExtractor
    # wraps only anthropic errors, so a bare RuntimeError from the fake
    # `.parse()` call propagates out of extractor.extract() uncaught by
    # run_pipeline's narrower `except KnowledgeExtractionError`, which is
    # exactly the "unexpected tick failure" build_tick's broad except must
    # still swallow.
    failing_client = _FakeClient(raise_error=True)
    collector, tick = _make_tick(tmp_path, failing_client)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await tick()  # must not raise

    assert any("scheduled sync tick failed" in r.getMessage() for r in caplog.records if r.name == LOGGER_NAME)


def test_default_interval_is_ten_minutes():
    assert DEFAULT_INTERVAL_SECONDS == 600


async def test_run_scheduled_sync_requires_root_or_collector():
    # Both validation branches raise before scheduler.start()'s forever loop,
    # so this is safe to await directly (unlike a real run_scheduled_sync()
    # call, which never returns).
    with pytest.raises(ValueError, match="root.*collector"):
        await run_scheduled_sync()


async def test_run_scheduled_sync_rejects_both_root_and_collector(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        await run_scheduled_sync(root=tmp_path, collector=LocalFileCollector(root=tmp_path))


async def test_trigger_now_reschedules_job_to_run_immediately():
    scheduler = AsyncIOScheduler()
    far_future = datetime.now() + timedelta(hours=1)
    scheduler.add_job(lambda: None, "interval", seconds=3600, next_run_time=far_future, id=SYNC_JOB_ID)
    scheduler.start(paused=True)
    try:
        trigger_now(scheduler)
        next_run_time = scheduler.get_job(SYNC_JOB_ID).next_run_time
        assert next_run_time < far_future.astimezone(next_run_time.tzinfo)
        assert (datetime.now(next_run_time.tzinfo) - next_run_time) < timedelta(seconds=5)
    finally:
        scheduler.shutdown(wait=False)


async def test_trigger_app_post_trigger_reschedules_job_to_run_immediately():
    from fastapi.testclient import TestClient

    scheduler = AsyncIOScheduler()
    far_future = datetime.now() + timedelta(hours=1)
    scheduler.add_job(lambda: None, "interval", seconds=3600, next_run_time=far_future, id=SYNC_JOB_ID)
    scheduler.start(paused=True)
    try:
        client = TestClient(build_trigger_app(scheduler))

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        response = client.post("/trigger")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "triggered"

        next_run_time = scheduler.get_job(SYNC_JOB_ID).next_run_time
        assert next_run_time < far_future.astimezone(next_run_time.tzinfo)
        assert next_run_time.isoformat() == body["next_run_time"]
    finally:
        scheduler.shutdown(wait=False)


DSN = os.environ.get("KF_TEST_DATABASE_URL")


@pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")
async def test_tick_persists_to_store_when_store_factory_given(tmp_path, caplog):
    from knowledge_forge.storage.store import PipelineStore

    (tmp_path / "a.txt").write_text("Persisted via the scheduler tick.")
    client = _FakeClient()
    collector, tick = _make_tick(
        tmp_path, client, store_factory=lambda: PipelineStore(DSN, tmp_path / "raw-data")
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await tick()

    assert _produced_count(caplog) >= 1

    uri = f"file://{(tmp_path / 'a.txt').resolve()}"
    async with PipelineStore(DSN, tmp_path / "raw-data") as store:
        conn = store._db.conn
        row = await (await conn.execute("SELECT source_id FROM sources WHERE uri = %s", (uri,))).fetchone()
        assert row is not None
        source_id = row[0]
        # FK-safe cleanup order: children before the parent `sources` row.
        await conn.execute("DELETE FROM knowledge_objects WHERE source_id = %s", (source_id,))
        await conn.execute("DELETE FROM chunks WHERE source_id = %s", (source_id,))
        await conn.execute("DELETE FROM documents WHERE source_id = %s", (source_id,))
        await conn.execute("DELETE FROM sources WHERE source_id = %s", (source_id,))
