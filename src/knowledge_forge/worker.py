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
from datetime import datetime
from pathlib import Path

from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS, job_type_for_source
from knowledge_forge.ingest import run_local_ingest, run_saas_ingest
from knowledge_forge.okf import OKFWriter
from knowledge_forge.storage.job_store import Job, JobStore
from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)

# job_type -> source, the reverse of job_type_for_source() - generated from
# SAAS_COLLECTOR_BUILDERS so a new SaaS collector doesn't need a matching
# change here.
_SAAS_JOB_TYPES = {job_type_for_source(source): source for source in SAAS_COLLECTOR_BUILDERS}


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


async def _dispatch(job: Job, database_url: str, data_dir: str) -> dict:
    max_tokens = job.payload.get("max_tokens", 4000)
    model = job.payload.get("model")
    # Opt-in, like extract/schedule's --okf-dir: only set when the /ingest
    # request included one. A job is a one-shot run (like extract, not
    # schedule's repeating tick), so run_local_ingest/run_saas_ingest write
    # the manifest/views once, at the end - see ingest.py's module docstring.
    okf_dir = job.payload.get("okf_dir")
    okf_writer = OKFWriter(okf_dir, project_id=job.project_id) if okf_dir else None
    # Opt-in backfill window (api/routes/ingest.py's IngestRequest.since/
    # until), round-tripped through the job payload as ISO strings since
    # `payload` must be JSON-safe - see pipeline.py's since/until docstring.
    since = _parse_datetime(job.payload.get("since"))
    until = _parse_datetime(job.payload.get("until"))

    if job.job_type == job_type_for_source("local"):
        path = job.payload["path"]
        async with PipelineStore(database_url, data_dir) as store:
            await store.init_schema()  # idempotent (CREATE TABLE IF NOT EXISTS)
            return await run_local_ingest(
                Path(path),
                max_tokens,
                model,
                store,
                project_id=job.project_id,
                okf_writer=okf_writer,
                since=since,
                until=until,
            )

    source = _SAAS_JOB_TYPES.get(job.job_type)
    if source is not None:
        async with PipelineStore(database_url, data_dir) as store:
            await store.init_schema()
            return await run_saas_ingest(
                source,
                max_tokens,
                model,
                store,
                project_id=job.project_id,
                okf_writer=okf_writer,
                since=since,
                until=until,
            )

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
