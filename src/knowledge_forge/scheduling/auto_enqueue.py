"""Auto-scheduling for `serve`: periodically enqueues ingest jobs into the
same `jobs` queue `POST /ingest` writes to, from inside the API process
itself (see api/app.py's lifespan) - starting `knowledge-forge serve` is
then enough to also start periodic sync, with no separate
`knowledge-forge schedule` process or docker-compose service to run by hand.

Deliberately enqueues rather than running the pipeline itself, unlike
`scheduling/scheduler.py`'s `schedule` CLI (which drives run_pipeline
directly, in-process): every ingest - scheduled or on-demand - goes through
the exact same `jobs` table -> Celery -> worker.py `_dispatch()` path, so
there is exactly one place that knows how to actually turn a source into
Knowledge Objects. Workers scale horizontally (wiki/api.md) independently of
how many sources are scheduled here, and adding a new scheduled source is a
config change, not a new process to deploy.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import BaseModel, model_validator

from knowledge_forge.celery_app import run_ingest_job
from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS, job_type_for_source
from knowledge_forge.storage.job_store import JobStore

logger = logging.getLogger(__name__)

# Mirrors scheduling/scheduler.py's DEFAULT_INTERVAL_SECONDS - kept as its
# own constant (rather than imported) since the two schedulers are
# independent knobs an operator could reasonably want to set differently.
DEFAULT_INTERVAL_SECONDS = 600


class ScheduledSource(BaseModel):
    """One entry of `KF_SCHEDULED_SOURCES` - the same fields `POST /ingest`
    (IngestRequest) accepts, plus `interval_seconds` for how often to
    re-enqueue it."""

    source: str = "local"
    path: Optional[str] = None
    project_id: str = "default"
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    max_tokens: Optional[int] = None
    model: Optional[str] = None
    okf_dir: Optional[str] = None

    @model_validator(mode="after")
    def _validate(self) -> "ScheduledSource":
        known_sources = {"local", *SAAS_COLLECTOR_BUILDERS}
        if self.source not in known_sources:
            raise ValueError(f"unknown source '{self.source}' (must be one of {sorted(known_sources)})")
        if self.source == "local" and not self.path:
            raise ValueError("source 'local' requires 'path'")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        return self


def parse_scheduled_sources(raw: Optional[str]) -> list[ScheduledSource]:
    """`raw` is `KF_SCHEDULED_SOURCES`: an inline JSON array, or a path to a
    file containing one - the same inline-or-path convention
    `collectors/saas/factory.py::_load_json_config` uses for a single JSON
    object (not reused directly here since this value is a list). None or
    blank disables auto-scheduling entirely - the default, so `serve` starts
    with no auto-scheduling unless explicitly configured.

    Raises ValueError/pydantic.ValidationError on malformed config, by
    design - a bad KF_SCHEDULED_SOURCES should fail `serve` startup loudly
    rather than silently schedule nothing or the wrong thing.
    """
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(Path(raw).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("KF_SCHEDULED_SOURCES must be a JSON array of source objects")
    return [ScheduledSource.model_validate(item) for item in data]


def _payload(source: ScheduledSource) -> dict:
    payload: dict = {}
    if source.source == "local":
        payload["path"] = source.path
    if source.max_tokens is not None:
        payload["max_tokens"] = source.max_tokens
    if source.model is not None:
        payload["model"] = source.model
    if source.okf_dir is not None:
        payload["okf_dir"] = source.okf_dir
    return payload


def _build_tick(dsn: str, source: ScheduledSource):
    """Returns a zero-arg async tick function that enqueues exactly one job
    for `source` - separate from `start_auto_enqueue_scheduler` so it can be
    unit-tested by awaiting it directly, the same split `scheduling/
    scheduler.py::build_tick` makes for the same reason.

    Opens its own JobStore connection per tick (rather than holding one for
    the scheduler's lifetime) - same self-healing-across-reconnects
    rationale as scheduler.py's per-tick store_factory and worker.py's
    per-job PipelineStore.
    """
    job_type = job_type_for_source(source.source)
    payload = _payload(source)

    async def _tick() -> None:
        try:
            async with JobStore(dsn) as job_store:
                job = await job_store.enqueue(job_type, payload, project_id=source.project_id)
            run_ingest_job.apply_async(args=[job.job_id], task_id=job.job_id)
            logger.info(
                "auto-scheduled enqueue: job_type=%s project_id=%s job_id=%s",
                job_type,
                source.project_id,
                job.job_id,
            )
        except Exception:  # noqa: BLE001 - one bad tick must not kill the scheduler
            logger.exception(
                "auto-scheduled enqueue failed for source=%s project_id=%s", source.source, source.project_id
            )

    return _tick


def start_auto_enqueue_scheduler(dsn: str, sources: list[ScheduledSource]) -> AsyncIOScheduler:
    """Starts one AsyncIOScheduler, on the caller's already-running event
    loop, with one repeating job per `sources` entry - each firing
    immediately then every `interval_seconds` (mirrors scheduler.py's
    `next_run_time=datetime.now()` choice, so the first sync doesn't wait a
    full interval after `serve` starts).

    The caller (api/app.py's lifespan) owns the returned scheduler's
    lifetime and must call `.shutdown()` on server shutdown.
    """
    scheduler = AsyncIOScheduler()
    for i, source in enumerate(sources):
        scheduler.add_job(
            _build_tick(dsn, source),
            "interval",
            seconds=source.interval_seconds,
            next_run_time=datetime.now(),
            id=f"auto_enqueue:{i}:{source.source}:{source.project_id}",
            # APScheduler's default misfire_grace_time is 1 second - on a
            # process also serving HTTP requests (unlike `schedule`'s
            # dedicated event loop), that's tight enough for a briefly busy
            # loop to blow past it, and APScheduler responds by *skipping*
            # that tick entirely rather than running it late (observed live:
            # a single delayed GET /health was enough to silently drop a
            # whole enqueue cycle). None disables the grace-period check, so
            # a late tick still enqueues instead of an entire sync cycle
            # vanishing with no trace besides a log line.
            misfire_grace_time=None,
        )
    scheduler.start()
    logger.info("auto-enqueue scheduler started with %d scheduled source(s)", len(sources))
    return scheduler
