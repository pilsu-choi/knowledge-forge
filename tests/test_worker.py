"""Integration test for worker._dispatch against a real PostgreSQL instance
(the worker's own JobStore.claim_next loop isn't unit-tested for the same
reason scheduling/scheduler.py's run_scheduled_sync isn't - it runs forever;
_dispatch is the isolated, awaitable unit, mirroring build_tick's role in
tests/scheduling/test_scheduler.py).

Skipped unless KF_TEST_DATABASE_URL is set.
"""

import os
from types import SimpleNamespace

import pytest

from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.storage.job_store import JobStore
from knowledge_forge.worker import _dispatch

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")


class _FakeMessages:
    async def parse(self, **kwargs):
        return SimpleNamespace(
            parsed_output=ExtractionResult(
                summary="stub summary", facts=[], entities=[], relations=[], topics=[], document_type="text"
            )
        )


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


async def test_dispatch_runs_local_file_ingest_and_persists(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("# A\n\nHello world.")

    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=_FakeClient())
    )

    async with JobStore(DSN) as job_store:
        await job_store.init_schema()
        job = await job_store.enqueue("local_file_ingest", {"path": str(tmp_path), "max_tokens": 4000})
        try:
            result = await _dispatch(job, DSN, str(tmp_path / "raw-data"))
            assert result["object_count"] >= 1

            uri = f"file://{(tmp_path / 'a.md').resolve()}"
            conn = job_store.conn
            row = await (await conn.execute("SELECT source_id FROM sources WHERE uri = %s", (uri,))).fetchone()
            assert row is not None
            source_id = row[0]
            await conn.execute("DELETE FROM knowledge_objects WHERE source_id = %s", (source_id,))
            await conn.execute("DELETE FROM chunks WHERE source_id = %s", (source_id,))
            await conn.execute("DELETE FROM documents WHERE source_id = %s", (source_id,))
            await conn.execute("DELETE FROM sources WHERE source_id = %s", (source_id,))
        finally:
            await job_store.conn.execute("DELETE FROM jobs WHERE job_id = %s", (job.job_id,))


async def test_dispatch_raises_on_unknown_job_type():
    from datetime import datetime, timezone

    from knowledge_forge.storage.job_store import Job, JobStatus

    job = Job(
        job_id="x",
        job_type="mystery_type",
        payload={},
        status=JobStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError, match="unknown job_type"):
        await _dispatch(job, DSN, "/tmp")
