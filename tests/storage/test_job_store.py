"""Integration tests against a real PostgreSQL instance.

Skipped unless KF_TEST_DATABASE_URL is set - same pattern as
tests/storage/test_postgres_store.py. To run these locally:

    docker compose up -d postgres
    KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests/storage/test_job_store.py -v
"""

import asyncio
import os

import pytest

from knowledge_forge.storage.job_store import JobStatus, JobStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")


async def _cleanup(store: JobStore, job_id: str) -> None:
    await store.conn.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))


@pytest.fixture(autouse=True)
async def _clear_jobs_table():
    # This DSN is a shared dev/CI Postgres, not a per-test sandbox - a stray
    # pending row left by a manual `curl /ingest` or a previously failed run
    # would otherwise get picked up by claim_next() instead of the row a
    # test just created, since claim_next() is (by design) "oldest pending,
    # regardless of who created it". Clearing before each test makes these
    # tests independent of whatever else has touched this table.
    async with JobStore(DSN) as store:
        await store.init_schema()
        await store.conn.execute("DELETE FROM jobs")
    yield


async def test_enqueue_and_get_roundtrip():
    async with JobStore(DSN) as store:
        await store.init_schema()
        job = await store.enqueue("local_file_ingest", {"path": "/tmp/x.md"})
        try:
            assert job.status == JobStatus.PENDING
            assert job.payload == {"path": "/tmp/x.md"}

            fetched = await store.get(job.job_id)
            assert fetched is not None
            assert fetched.job_id == job.job_id
            assert fetched.status == JobStatus.PENDING
        finally:
            await _cleanup(store, job.job_id)


async def test_get_missing_returns_none():
    async with JobStore(DSN) as store:
        await store.init_schema()
        assert await store.get("does-not-exist") is None


async def test_list_filters_by_status():
    async with JobStore(DSN) as store:
        await store.init_schema()
        job = await store.enqueue("local_file_ingest", {"path": "/tmp/y.md"})
        try:
            pending = await store.list(status=JobStatus.PENDING, limit=200)
            assert any(j.job_id == job.job_id for j in pending)

            succeeded = await store.list(status=JobStatus.SUCCEEDED, limit=200)
            assert not any(j.job_id == job.job_id for j in succeeded)
        finally:
            await _cleanup(store, job.job_id)


async def test_enqueue_defaults_project_id_and_list_filters_by_it():
    async with JobStore(DSN) as store:
        await store.init_schema()
        job_default = await store.enqueue("local_file_ingest", {"path": "/tmp/d.md"})
        job_a = await store.enqueue("local_file_ingest", {"path": "/tmp/a.md"}, project_id="proj-a")
        try:
            assert job_default.project_id == "default"
            assert job_a.project_id == "proj-a"

            proj_a_jobs = await store.list(project_id="proj-a", limit=200)
            assert {j.job_id for j in proj_a_jobs} == {job_a.job_id}
        finally:
            await _cleanup(store, job_default.job_id)
            await _cleanup(store, job_a.job_id)


async def test_claim_next_marks_running_and_skips_already_claimed():
    async with JobStore(DSN) as store:
        await store.init_schema()
        job = await store.enqueue("local_file_ingest", {"path": "/tmp/z.md"})
        try:
            claimed = await store.claim_next()
            assert claimed is not None
            assert claimed.job_id == job.job_id
            assert claimed.status == JobStatus.RUNNING
            assert claimed.started_at is not None

            # No other pending job with this id - re-claiming shouldn't pick
            # a running job back up. (Another queued job could race in from
            # a different test process, so only assert on our own job_id.)
            again = await store.get(job.job_id)
            assert again.status == JobStatus.RUNNING
        finally:
            await _cleanup(store, job.job_id)


async def test_claim_next_is_concurrency_safe_across_connections():
    async with JobStore(DSN) as store_a, JobStore(DSN) as store_b:
        await store_a.init_schema()
        job1 = await store_a.enqueue("local_file_ingest", {"path": "/tmp/c1.md"})
        job2 = await store_a.enqueue("local_file_ingest", {"path": "/tmp/c2.md"})
        try:
            claimed_a, claimed_b = await asyncio.gather(store_a.claim_next(), store_b.claim_next())
            assert claimed_a is not None and claimed_b is not None
            assert {claimed_a.job_id, claimed_b.job_id} == {job1.job_id, job2.job_id}
        finally:
            await _cleanup(store_a, job1.job_id)
            await _cleanup(store_a, job2.job_id)


async def test_mark_succeeded_and_mark_failed():
    async with JobStore(DSN) as store:
        await store.init_schema()
        job_ok = await store.enqueue("local_file_ingest", {"path": "/tmp/ok.md"})
        job_bad = await store.enqueue("local_file_ingest", {"path": "/tmp/bad.md"})
        try:
            await store.mark_succeeded(job_ok.job_id, {"object_count": 3})
            ok = await store.get(job_ok.job_id)
            assert ok.status == JobStatus.SUCCEEDED
            assert ok.result == {"object_count": 3}
            assert ok.finished_at is not None

            await store.mark_failed(job_bad.job_id, "boom")
            bad = await store.get(job_bad.job_id)
            assert bad.status == JobStatus.FAILED
            assert bad.error == "boom"
            assert bad.finished_at is not None
        finally:
            await _cleanup(store, job_ok.job_id)
            await _cleanup(store, job_bad.job_id)
