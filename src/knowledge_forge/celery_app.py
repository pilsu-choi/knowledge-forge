"""Celery wiring: the transport that carries a job from `POST /ingest`
(api/routes/ingest.py) or an auto-scheduled tick (scheduling/auto_enqueue.py)
to a `knowledge-forge worker` process, over Redis - see wiki/api.md.

The `jobs` Postgres table (storage/job_store.py) stays the source of truth
for job status/results; Celery/Redis only decides *when* and *on which
worker* a job actually runs, and gives Flower something to monitor
(`celery -A knowledge_forge.celery_app flower`, or the `flower`
docker-compose service).
"""

import asyncio

from celery import Celery

from knowledge_forge.config import settings
from knowledge_forge.worker import _execute_job

celery_app = Celery("knowledge_forge", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_track_started=True,
    # Job bookkeeping already lives in the `jobs` table (result/error columns)
    # - the Redis result backend is only used for Flower/AsyncResult
    # introspection, so results don't need to outlive a debugging session.
    result_expires=3600,
)


@celery_app.task(name="knowledge_forge.run_ingest_job")
def run_ingest_job(job_id: str) -> dict:
    """Runs inside a Celery worker process (prefork by default) - each
    invocation gets its own event loop via `asyncio.run`, mirroring how
    `scheduling/scheduler.py` and `scheduling/auto_enqueue.py` each open a
    fresh store connection per tick rather than sharing one across calls."""
    if not settings.database_url:
        raise RuntimeError("KF_DATABASE_URL is not set - a worker needs Postgres to read/write jobs.")
    return asyncio.run(_execute_job(job_id, settings.database_url, settings.data_dir))
