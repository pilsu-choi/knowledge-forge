"""Job-queue worker: the "process" half of Ingest/Process separation
(wiki/roadmap.md 4단계, wiki/api.md). The FastAPI server's `POST /ingest`
enqueues a row and returns immediately; this polls `jobs` with
`JobStore.claim_next()` (Postgres `FOR UPDATE SKIP LOCKED`) and actually
runs the pipeline - a separate, restartable process, deployed as its own
docker-compose service.

Same failure-isolation philosophy as `scheduling/scheduler.py`'s tick: one
job's exception is recorded on that job (status=failed) and the loop moves
on, rather than crashing the whole worker process.
"""

import asyncio
import logging
from pathlib import Path

from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS
from knowledge_forge.ingest import run_local_ingest, run_saas_ingest
from knowledge_forge.storage.job_store import Job, JobStore
from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)

# job_type naming: "local_file_ingest" predates the `{source}_ingest`
# convention (api/routes/ingest.py's `source` field) and keeps its own name
# for backward compatibility with already-enqueued/persisted jobs; every
# SaaS source instead gets "{source}_ingest" (e.g. "slack_ingest"),
# generated from SAAS_COLLECTOR_BUILDERS so a new SaaS collector doesn't need
# a matching change here.
_SAAS_JOB_TYPES = {f"{source}_ingest": source for source in SAAS_COLLECTOR_BUILDERS}


async def _dispatch(job: Job, database_url: str, data_dir: str) -> dict:
    max_tokens = job.payload.get("max_tokens", 4000)
    model = job.payload.get("model")

    if job.job_type == "local_file_ingest":
        path = job.payload["path"]
        async with PipelineStore(database_url, data_dir) as store:
            await store.init_schema()  # idempotent (CREATE TABLE IF NOT EXISTS)
            return await run_local_ingest(Path(path), max_tokens, model, store, project_id=job.project_id)

    source = _SAAS_JOB_TYPES.get(job.job_type)
    if source is not None:
        async with PipelineStore(database_url, data_dir) as store:
            await store.init_schema()
            return await run_saas_ingest(source, max_tokens, model, store, project_id=job.project_id)

    raise ValueError(f"unknown job_type: {job.job_type}")


async def run_worker(
    database_url: str,
    data_dir: str,
    poll_interval_seconds: float = 2.0,
) -> None:
    """Runs forever, polling `jobs` for pending work, until interrupted
    (Ctrl-C / SIGINT)."""
    async with JobStore(database_url) as job_store:
        await job_store.init_schema()
        logger.info("worker started, polling every %.1fs", poll_interval_seconds)
        try:
            while True:
                job = await job_store.claim_next()
                if job is None:
                    await asyncio.sleep(poll_interval_seconds)
                    continue
                logger.info("claimed job %s (%s)", job.job_id, job.job_type)
                try:
                    result = await _dispatch(job, database_url, data_dir)
                except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
                    logger.exception("job %s failed", job.job_id)
                    await job_store.mark_failed(job.job_id, str(exc))
                else:
                    await job_store.mark_succeeded(job.job_id, result)
                    logger.info("job %s succeeded: %s", job.job_id, result)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            logger.info("worker stopped")
