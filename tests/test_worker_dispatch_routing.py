"""_dispatch()'s job_type -> ingest-helper routing, without a real Postgres
(unlike test_worker.py's KF_TEST_DATABASE_URL-gated end-to-end test) - a fake
PipelineStore stands in so this always runs, covering the "{source}_ingest"
dispatch added alongside api/routes/ingest.py's `source` field.
"""

from datetime import datetime, timezone

import pytest

from knowledge_forge.storage.job_store import Job, JobStatus
from knowledge_forge.worker import _dispatch, _execute_job


class _FakePipelineStore:
    def __init__(self, database_url, data_dir):
        self.database_url = database_url
        self.data_dir = data_dir

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def init_schema(self):
        pass


def _job(job_type: str, payload: dict, project_id: str = "default") -> Job:
    return Job(
        job_id="j1",
        job_type=job_type,
        payload=payload,
        status=JobStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        project_id=project_id,
    )


async def test_dispatch_routes_slack_ingest_to_run_saas_ingest(monkeypatch):
    monkeypatch.setattr("knowledge_forge.worker.PipelineStore", _FakePipelineStore)

    calls = []

    async def fake_run_saas_ingest(source, max_tokens, model, store, project_id, okf_writer=None, since=None, until=None):
        calls.append((source, max_tokens, model, project_id, okf_writer))
        return {"object_count": 1, "relation_count": 0}

    monkeypatch.setattr("knowledge_forge.worker.run_saas_ingest", fake_run_saas_ingest)

    job = _job("slack_ingest", {"max_tokens": 2000, "model": "m"}, project_id="PRJ-A")
    result = await _dispatch(job, "postgresql://fake", "/tmp/data")

    assert result == {"object_count": 1, "relation_count": 0}
    assert calls == [("slack", 2000, "m", "PRJ-A", None)]


async def test_dispatch_defaults_max_tokens_and_model_when_absent(monkeypatch):
    monkeypatch.setattr("knowledge_forge.worker.PipelineStore", _FakePipelineStore)

    calls = []

    async def fake_run_saas_ingest(source, max_tokens, model, store, project_id, okf_writer=None, since=None, until=None):
        calls.append((source, max_tokens, model))
        return {}

    monkeypatch.setattr("knowledge_forge.worker.run_saas_ingest", fake_run_saas_ingest)

    job = _job("notion_ingest", {})
    await _dispatch(job, "postgresql://fake", "/tmp/data")

    assert calls == [("notion", 4000, None)]


async def test_dispatch_still_routes_local_file_ingest(monkeypatch, tmp_path):
    monkeypatch.setattr("knowledge_forge.worker.PipelineStore", _FakePipelineStore)

    calls = []

    async def fake_run_local_ingest(path, max_tokens, model, store, project_id, okf_writer=None, since=None, until=None):
        calls.append((str(path), max_tokens, model, project_id, okf_writer))
        return {}

    monkeypatch.setattr("knowledge_forge.worker.run_local_ingest", fake_run_local_ingest)

    job = _job("local_file_ingest", {"path": str(tmp_path), "max_tokens": 1000}, project_id="PRJ-B")
    await _dispatch(job, "postgresql://fake", "/tmp/data")

    assert calls == [(str(tmp_path), 1000, None, "PRJ-B", None)]


async def test_dispatch_builds_okf_writer_when_job_has_okf_dir(monkeypatch, tmp_path):
    from knowledge_forge.okf import OKFWriter

    monkeypatch.setattr("knowledge_forge.worker.PipelineStore", _FakePipelineStore)

    calls = []

    async def fake_run_local_ingest(path, max_tokens, model, store, project_id, okf_writer=None, since=None, until=None):
        calls.append(okf_writer)
        return {}

    monkeypatch.setattr("knowledge_forge.worker.run_local_ingest", fake_run_local_ingest)

    okf_dir = tmp_path / "okf"
    job = _job(
        "local_file_ingest",
        {"path": str(tmp_path), "okf_dir": str(okf_dir)},
        project_id="PRJ-C",
    )
    await _dispatch(job, "postgresql://fake", "/tmp/data")

    assert len(calls) == 1
    assert isinstance(calls[0], OKFWriter)
    # OKFWriter's __init__ eagerly creates the (fallback/default) project
    # subtree - confirms it was built with this job's project_id, not "default".
    assert (okf_dir / "PRJ-C").is_dir()


async def test_dispatch_raises_on_unknown_job_type():
    job = _job("mystery_type", {})
    with pytest.raises(ValueError, match="unknown job_type"):
        await _dispatch(job, "postgresql://fake", "/tmp/data")


async def test_dispatch_parses_since_until_from_payload_and_passes_to_run_saas_ingest(monkeypatch):
    """api/routes/ingest.py enqueues since/until as ISO strings (payload must be
    JSON-safe) - _dispatch must parse them back into datetimes before calling
    run_saas_ingest/run_local_ingest (see pipeline.py's since/until docstring)."""
    monkeypatch.setattr("knowledge_forge.worker.PipelineStore", _FakePipelineStore)

    calls = []

    async def fake_run_saas_ingest(source, max_tokens, model, store, project_id, okf_writer=None, since=None, until=None):
        calls.append((since, until))
        return {}

    monkeypatch.setattr("knowledge_forge.worker.run_saas_ingest", fake_run_saas_ingest)

    job = _job("slack_ingest", {"since": "2026-08-01T09:00:00+00:00", "until": "2026-08-01T18:00:00+00:00"})
    await _dispatch(job, "postgresql://fake", "/tmp/data")

    assert calls == [
        (datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc), datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc))
    ]


class _FakeJobStore:
    """Stands in for storage.job_store.JobStore - `_execute_job` opens one of
    these itself (see worker.py's per-job self-healing rationale), so it's
    monkeypatched at the module level, same as `_FakePipelineStore` above."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self.calls: list[tuple] = []

    async def __aenter__(self) -> "_FakeJobStore":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def get(self, job_id: str) -> Job:
        self.calls.append(("get", job_id))
        return self._job

    async def mark_running(self, job_id: str) -> None:
        self.calls.append(("mark_running", job_id))

    async def mark_succeeded(self, job_id: str, result: dict) -> None:
        self.calls.append(("mark_succeeded", job_id, result))

    async def mark_failed(self, job_id: str, error: str) -> None:
        self.calls.append(("mark_failed", job_id, error))


async def test_execute_job_marks_running_then_succeeded(monkeypatch):
    monkeypatch.setattr("knowledge_forge.worker.PipelineStore", _FakePipelineStore)

    async def fake_run_local_ingest(path, max_tokens, model, store, project_id, okf_writer=None, since=None, until=None):
        return {"object_count": 1}

    monkeypatch.setattr("knowledge_forge.worker.run_local_ingest", fake_run_local_ingest)

    fake_store = _FakeJobStore("postgresql://fake")
    fake_store._job = _job("local_file_ingest", {"path": "/tmp/x"})
    monkeypatch.setattr("knowledge_forge.worker.JobStore", lambda dsn: fake_store)

    result = await _execute_job("j1", "postgresql://fake", "/tmp/data")

    assert result == {"object_count": 1}
    assert fake_store.calls == [
        ("get", "j1"),
        ("mark_running", "j1"),
        ("mark_succeeded", "j1", {"object_count": 1}),
    ]


async def test_execute_job_marks_failed_and_reraises_on_dispatch_error(monkeypatch):
    fake_store = _FakeJobStore("postgresql://fake")
    fake_store._job = _job("mystery_type", {})
    monkeypatch.setattr("knowledge_forge.worker.JobStore", lambda dsn: fake_store)

    with pytest.raises(ValueError, match="unknown job_type"):
        await _execute_job("j1", "postgresql://fake", "/tmp/data")

    assert fake_store.calls[:2] == [("get", "j1"), ("mark_running", "j1")]
    assert fake_store.calls[2][:2] == ("mark_failed", "j1")
    assert "unknown job_type" in fake_store.calls[2][2]


async def test_dispatch_defaults_since_until_to_none_when_absent(monkeypatch):
    monkeypatch.setattr("knowledge_forge.worker.PipelineStore", _FakePipelineStore)

    calls = []

    async def fake_run_saas_ingest(source, max_tokens, model, store, project_id, okf_writer=None, since=None, until=None):
        calls.append((since, until))
        return {}

    monkeypatch.setattr("knowledge_forge.worker.run_saas_ingest", fake_run_saas_ingest)

    job = _job("slack_ingest", {})
    await _dispatch(job, "postgresql://fake", "/tmp/data")

    assert calls == [(None, None)]
