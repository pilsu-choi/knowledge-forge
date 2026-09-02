"""Tests for auto_enqueue's parsing and per-source tick logic, without a
real Postgres or Redis (a fake JobStore and a fake Celery task stand in -
same pattern tests/test_worker_dispatch_routing.py uses for PipelineStore).
The actual AsyncIOScheduler wiring (start_auto_enqueue_scheduler) is
exercised without waiting for a real fire, same as
tests/scheduling/test_scheduler.py does for `schedule`'s scheduler.
"""

import json
import logging

import pytest
from pydantic import ValidationError

from knowledge_forge.scheduling.auto_enqueue import (
    DEFAULT_INTERVAL_SECONDS,
    ScheduledSource,
    _build_tick,
    parse_scheduled_sources,
    start_auto_enqueue_scheduler,
)

LOGGER_NAME = "knowledge_forge.scheduling.auto_enqueue"


def test_parse_empty_or_none_disables_scheduling():
    assert parse_scheduled_sources(None) == []
    assert parse_scheduled_sources("") == []
    assert parse_scheduled_sources("   ") == []


def test_parse_inline_json_array():
    raw = json.dumps([{"source": "local", "path": "/app/input", "interval_seconds": 300}])
    sources = parse_scheduled_sources(raw)
    assert sources == [ScheduledSource(source="local", path="/app/input", interval_seconds=300)]


def test_parse_defaults_interval_when_omitted():
    sources = parse_scheduled_sources(json.dumps([{"source": "local", "path": "/app/input"}]))
    assert sources[0].interval_seconds == DEFAULT_INTERVAL_SECONDS


def test_parse_reads_from_file_path(tmp_path):
    config_file = tmp_path / "scheduled.json"
    config_file.write_text(json.dumps([{"source": "local", "path": "/app/input"}]))
    sources = parse_scheduled_sources(str(config_file))
    assert sources[0].path == "/app/input"


def test_parse_rejects_non_array_json():
    with pytest.raises(ValueError, match="JSON array"):
        parse_scheduled_sources(json.dumps({"source": "local", "path": "/app/input"}))


def test_parse_rejects_unknown_source():
    with pytest.raises(ValidationError, match="unknown source"):
        parse_scheduled_sources(json.dumps([{"source": "carrier-pigeon"}]))


def test_parse_rejects_local_without_path():
    with pytest.raises(ValidationError, match="requires 'path'"):
        parse_scheduled_sources(json.dumps([{"source": "local"}]))


def test_parse_rejects_non_positive_interval():
    with pytest.raises(ValidationError, match="interval_seconds must be positive"):
        parse_scheduled_sources(json.dumps([{"source": "local", "path": "/app/input", "interval_seconds": 0}]))


def test_parse_accepts_saas_source_without_path():
    sources = parse_scheduled_sources(json.dumps([{"source": "slack", "project_id": "PRJ-A"}]))
    assert sources == [ScheduledSource(source="slack", project_id="PRJ-A")]


class _FakeJob:
    def __init__(self, job_id: str):
        self.job_id = job_id


class _FakeJobStore:
    enqueued: list[tuple[str, dict, str]] = []

    def __init__(self, dsn: str):
        self._dsn = dsn

    async def __aenter__(self) -> "_FakeJobStore":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def enqueue(self, job_type: str, payload: dict, project_id: str = "default"):
        _FakeJobStore.enqueued.append((job_type, payload, project_id))
        return _FakeJob(job_id="job-1")


class _FakeCeleryTask:
    def __init__(self):
        self.dispatched: list[tuple] = []

    def apply_async(self, args, task_id):
        self.dispatched.append((args, task_id))


@pytest.fixture(autouse=True)
def _reset_fake_job_store():
    _FakeJobStore.enqueued = []
    yield


@pytest.fixture(autouse=True)
def _fake_run_ingest_job(monkeypatch):
    fake_task = _FakeCeleryTask()
    monkeypatch.setattr("knowledge_forge.scheduling.auto_enqueue.run_ingest_job", fake_task)
    return fake_task


async def test_tick_enqueues_local_job_with_expected_payload(monkeypatch, _fake_run_ingest_job):
    monkeypatch.setattr("knowledge_forge.scheduling.auto_enqueue.JobStore", _FakeJobStore)
    source = ScheduledSource(source="local", path="/app/input", project_id="PRJ-A", max_tokens=2000, okf_dir="/app/okf")

    await _build_tick("postgresql://fake", source)()

    assert _FakeJobStore.enqueued == [
        ("local_file_ingest", {"path": "/app/input", "max_tokens": 2000, "okf_dir": "/app/okf"}, "PRJ-A")
    ]
    # enqueue() returns job_id="job-1" (see _FakeJobStore) - the tick must
    # hand that same id to Celery so Flower and GET /jobs/{id} agree.
    assert _fake_run_ingest_job.dispatched == [(["job-1"], "job-1")]


async def test_tick_enqueues_saas_job_without_path(monkeypatch):
    monkeypatch.setattr("knowledge_forge.scheduling.auto_enqueue.JobStore", _FakeJobStore)
    source = ScheduledSource(source="slack", project_id="PRJ-B")

    await _build_tick("postgresql://fake", source)()

    job_type, payload, project_id = _FakeJobStore.enqueued[0]
    assert job_type == "slack_ingest"
    assert "path" not in payload
    assert project_id == "PRJ-B"


async def test_tick_swallows_exceptions_and_logs_instead_of_raising(monkeypatch, caplog):
    class _BrokenJobStore(_FakeJobStore):
        async def enqueue(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr("knowledge_forge.scheduling.auto_enqueue.JobStore", _BrokenJobStore)
    source = ScheduledSource(source="local", path="/app/input")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        await _build_tick("postgresql://fake", source)()  # must not raise

    assert any("auto-scheduled enqueue failed" in r.getMessage() for r in caplog.records if r.name == LOGGER_NAME)


async def test_start_auto_enqueue_scheduler_registers_one_job_per_source():
    sources = [
        ScheduledSource(source="local", path="/app/input", interval_seconds=60),
        ScheduledSource(source="slack", project_id="PRJ-A", interval_seconds=120),
    ]
    scheduler = start_auto_enqueue_scheduler("postgresql://fake", sources)
    try:
        jobs = scheduler.get_jobs()
        assert len(jobs) == 2
        assert {job.trigger.interval.total_seconds() for job in jobs} == {60.0, 120.0}
        # Regression: APScheduler's default misfire_grace_time (1s) is tight
        # enough that a briefly busy event loop (this process also serves
        # HTTP requests, unlike `schedule`'s dedicated loop) makes it skip a
        # tick outright instead of running it late - observed live via
        # docker compose (a single delayed GET /health silently dropped a
        # whole enqueue cycle). Must be disabled (None) so a late tick still
        # runs.
        assert all(job.misfire_grace_time is None for job in jobs)
    finally:
        scheduler.shutdown(wait=False)


async def test_start_auto_enqueue_scheduler_with_no_sources_registers_nothing():
    scheduler = start_auto_enqueue_scheduler("postgresql://fake", [])
    try:
        assert scheduler.get_jobs() == []
    finally:
        scheduler.shutdown(wait=False)
