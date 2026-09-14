"""In-process scheduling for periodic incremental sync.

Runs the same Collector -> Parser -> Adaptive Chunker -> Knowledge
Extraction pipeline as `knowledge-forge extract`, but on a repeating
interval inside one long-lived process - an alternative to wiring an
external cron/systemd timer around the one-shot CLI command.
"""

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.config import settings
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline

if TYPE_CHECKING:
    from fastapi import FastAPI

    from knowledge_forge.okf import OKFWriter
    from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)

# Default incremental-sync interval when the caller (CLI's `schedule`
# command) doesn't override it - 10 minutes.
DEFAULT_INTERVAL_SECONDS = 600

# Fixed id for the (single) recurring sync job so trigger_now() can look it
# up via AsyncIOScheduler.modify_job() instead of tracking the Job object.
SYNC_JOB_ID = "sync_tick"

# Bound to localhost by default - the trigger endpoint has no auth, so it's
# opt-in to expose beyond the local machine (via --trigger-host on the CLI).
DEFAULT_TRIGGER_API_HOST = "127.0.0.1"
DEFAULT_TRIGGER_API_PORT = 8765


def build_tick(
    collector: Collector,
    registry,
    chunker,
    extractor,
    store_factory: Callable[[], "PipelineStore"] | None = None,
    project_id: str = "default",
    okf_writer: "OKFWriter | None" = None,
) -> Callable[[], Coroutine[None, None, None]]:
    """Returns a zero-arg async tick function that runs one incremental sync pass.

    Exposed separately from run_scheduled_sync so it can be unit-tested by
    awaiting it directly, bypassing AsyncIOScheduler's (permanently running)
    event loop entirely.

    `collector.incremental_sync(collector.get_cursor())` behaves correctly
    even on the very first call - with no prior persisted state,
    get_cursor() returns an empty manifest, so every file comes back as
    "new" (the same effect as initial_sync()). No first-tick special case
    is needed; every tick, including the first, just does this uniformly
    via run_pipeline(..., cursor=collector.get_cursor()).

    Each tick opens its own store connection (via store_factory, a
    zero-arg callable returning a fresh PipelineStore) and closes it when
    the tick finishes, rather than holding one Postgres connection open
    across the whole scheduler's lifetime - simpler lifecycle, and it
    self-heals from a connection dropped between ticks.

    `okf_writer` (opt-in, like `--okf-dir` on `extract`) gets `write_resource`/
    `write_objects`/`write_relations` calls from `run_pipeline` during the
    tick same as `extract`, and `write_manifest()`/`write_views()` once more
    at the end of *every* tick (not just once at process exit, unlike
    `extract`) - safe to call repeatedly since both rebuild by scanning the
    tree (see okf/writer.py), so each tick's manifest/views end up correct
    for everything collected so far, incremental sync included.
    """

    async def _tick() -> None:
        try:
            cursor = collector.get_cursor()
            if store_factory is not None:
                async with store_factory() as store:
                    await store.init_schema()  # idempotent (CREATE TABLE IF NOT EXISTS)
                    outcomes = [
                        outcome
                        async for outcome in run_pipeline(
                            collector,
                            registry,
                            chunker,
                            extractor,
                            cursor=cursor,
                            store=store,
                            okf_writer=okf_writer,
                            project_id=project_id,
                        )
                    ]
            else:
                outcomes = [
                    outcome
                    async for outcome in run_pipeline(
                        collector,
                        registry,
                        chunker,
                        extractor,
                        cursor=cursor,
                        store=None,
                        okf_writer=okf_writer,
                        project_id=project_id,
                    )
                ]
            if okf_writer is not None:
                okf_writer.write_manifest()
                okf_writer.write_views()
            object_count = sum(len(outcome.objects) for outcome in outcomes)
            logger.info("scheduled sync tick produced %d Knowledge Object(s)", object_count)
        except Exception:  # noqa: BLE001 - one failed tick must not kill the scheduler
            logger.exception("scheduled sync tick failed")

    return _tick


def trigger_now(scheduler: AsyncIOScheduler) -> None:
    """Runs the recurring sync job immediately instead of waiting for its
    next interval-based fire time, by re-pointing its `next_run_time` at
    now. Reschedules the *existing* job (rather than firing the tick
    function directly) so APScheduler's own executor still serializes it
    against a tick that's already in flight - no risk of two ticks running
    concurrently just because a manual trigger landed mid-interval.

    The job's subsequent fire times are computed from the interval
    trigger's original start date, so a manual trigger doesn't shift the
    regular cadence going forward - it just runs one extra time now.
    """
    scheduler.modify_job(SYNC_JOB_ID, next_run_time=datetime.now())
    logger.info("manual sync trigger requested - running now")


def _job_status(scheduler: AsyncIOScheduler) -> dict:
    job = scheduler.get_job(SYNC_JOB_ID)
    return {"next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None}


def build_trigger_app(scheduler: AsyncIOScheduler) -> "FastAPI":
    """A tiny standalone FastAPI app (not knowledge_forge.api.app's - that one
    belongs to the separate, request-driven `serve`/`worker` job queue, see
    wiki/scheduling.md "schedule vs. serve/worker") giving the `schedule`
    process an HTTP counterpart to the SIGUSR1 manual trigger: `POST
    /trigger` calls trigger_now(scheduler) exactly like the signal handler
    does, for operators who'd rather curl a local port than send a signal
    (e.g. from a container healthcheck-style sidecar).
    """
    from fastapi import FastAPI

    app = FastAPI(title="Knowledge Forge Scheduler Trigger")

    @app.post("/trigger")
    async def trigger() -> dict:
        trigger_now(scheduler)
        return {"status": "triggered", **_job_status(scheduler)}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", **_job_status(scheduler)}

    return app


async def _serve_trigger_api(scheduler: AsyncIOScheduler, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(build_trigger_app(scheduler), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


def _install_manual_trigger_handler(scheduler: AsyncIOScheduler) -> bool:
    """Wires SIGUSR1 to trigger_now(scheduler) so an operator can force an
    immediate sync (`kill -USR1 <pid>`) without waiting for the interval,
    without needing a control API for what is otherwise a plain foreground
    polling loop. Returns whether the handler was installed - SIGUSR1 (and
    asyncio's loop.add_signal_handler) isn't available on Windows, so
    run_scheduled_sync degrades gracefully there (interval-only, no manual
    trigger) rather than failing to start.
    """
    if not hasattr(signal, "SIGUSR1"):
        return False
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, lambda: trigger_now(scheduler))
    except NotImplementedError:
        return False
    return True


async def run_scheduled_sync(
    root: Path | str | None = None,
    collector: Collector | None = None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    max_tokens: int = 4000,
    model: str | None = None,
    store_factory: Callable[[], "PipelineStore"] | None = None,
    state_path: Path | str | None = None,
    project_id: str = "default",
    okf_writer: "OKFWriter | None" = None,
    trigger_api_host: str = DEFAULT_TRIGGER_API_HOST,
    trigger_api_port: int | None = DEFAULT_TRIGGER_API_PORT,
) -> None:
    """Runs forever as a background job on the current event loop,
    incrementally syncing every `interval_seconds` (firing once immediately
    at startup, then on the interval) until interrupted (Ctrl-C / SIGINT).
    An operator can also force an out-of-cycle sync at any time via
    `kill -USR1 <pid>`, or by POSTing to `/trigger` on the trigger API
    (bound to `trigger_api_host:trigger_api_port`, disabled by passing
    `trigger_api_port=None`) - see trigger_now()/build_trigger_app() and
    wiki/scheduling.md.

    Exactly one of `root` or `collector` must be given. `root` builds a
    `LocalFileCollector(root=root, state_path=state_path)` - `state_path`
    persists its cursor to disk, so incremental sync survives a process
    restart. `collector` takes an already-built Collector (e.g. from
    collectors/saas/factory.py) as-is; its cursor only lives in memory for
    this process's lifetime (see SlackCollector), since SaaS collectors have
    no `state_path` equivalent yet - restarting `schedule` re-runs their
    initial_sync().

    `okf_writer` is optional (like `extract`'s `--okf-dir`, which builds one
    the same way, `OKFWriter(okf_dir, project_id=project_id)`) - passed
    through to build_tick() as-is, see its docstring for why every tick, not
    just process exit, re-writes the manifest/views.
    """
    if collector is None:
        if root is None:
            raise ValueError("run_scheduled_sync() needs either `root` or `collector`.")
        collector = LocalFileCollector(root=root, state_path=state_path)
    elif root is not None:
        raise ValueError("run_scheduled_sync() takes `root` or `collector`, not both.")
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens, min_tokens=settings.min_tokens_per_chunk)
    # model=None lets get_extractor() pick the active provider's own
    # default model instead of always defaulting to an Anthropic model id.
    extractor = get_extractor(model=model)
    tick = build_tick(
        collector,
        registry,
        chunker,
        extractor,
        store_factory=store_factory,
        project_id=project_id,
        okf_writer=okf_writer,
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(tick, "interval", seconds=interval_seconds, next_run_time=datetime.now(), id=SYNC_JOB_ID)
    scheduler.start()
    if _install_manual_trigger_handler(scheduler):
        logger.info("manual sync trigger enabled - run `kill -USR1 %d` to sync immediately", os.getpid())

    api_task: asyncio.Task | None = None
    if trigger_api_port is not None:
        api_task = asyncio.create_task(_serve_trigger_api(scheduler, trigger_api_host, trigger_api_port))
        logger.info(
            "manual sync trigger API enabled - POST http://%s:%d/trigger to sync immediately",
            trigger_api_host,
            trigger_api_port,
        )

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        if api_task is not None:
            api_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await api_task
        logger.info("scheduler stopped")
