"""Tests the `run_ingest_job` Celery task in eager mode - `task_always_eager`
runs it synchronously in-process with no real Redis broker, the standard way
to unit-test a Celery task (see wiki/api.md)."""

import pytest

from knowledge_forge.celery_app import celery_app, run_ingest_job


@pytest.fixture(autouse=True)
def _eager_mode():
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


def test_run_ingest_job_calls_execute_job(monkeypatch):
    calls = []

    async def fake_execute_job(job_id, database_url, data_dir):
        calls.append((job_id, database_url, data_dir))
        return {"object_count": 1}

    monkeypatch.setattr("knowledge_forge.celery_app._execute_job", fake_execute_job)
    monkeypatch.setattr("knowledge_forge.celery_app.settings.database_url", "postgresql://fake")
    monkeypatch.setattr("knowledge_forge.celery_app.settings.data_dir", "/tmp/data")

    async_result = run_ingest_job.apply_async(args=["job-1"], task_id="job-1")

    assert async_result.get() == {"object_count": 1}
    assert calls == [("job-1", "postgresql://fake", "/tmp/data")]
