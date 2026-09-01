"""In-process scheduling for periodic incremental sync.

Runs the same Collector -> Parser -> Adaptive Chunker -> Knowledge
Extraction pipeline as `knowledge-forge extract`, but on a repeating
interval inside one long-lived process - an alternative to wiring an
external cron/systemd timer around the one-shot CLI command.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline

if TYPE_CHECKING:
    from knowledge_forge.storage.store import PipelineStore

logger = logging.getLogger(__name__)


def build_tick(
    collector: LocalFileCollector,
    registry,
    chunker,
    extractor,
    store_factory: Optional[Callable[[], "PipelineStore"]] = None,
) -> Callable[[], "asyncio.Future[None]"]:
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
                            collector, registry, chunker, extractor, cursor=cursor, store=store
                        )
                    ]
            else:
                outcomes = [
                    outcome
                    async for outcome in run_pipeline(collector, registry, chunker, extractor, cursor=cursor, store=None)
                ]
            object_count = sum(len(outcome.objects) for outcome in outcomes)
            logger.info("scheduled sync tick produced %d Knowledge Object(s)", object_count)
        except Exception:  # noqa: BLE001 - one failed tick must not kill the scheduler
            logger.exception("scheduled sync tick failed")

    return _tick


async def run_scheduled_sync(
    root: Path | str,
    interval_seconds: int,
    max_tokens: int,
    model: str | None = None,
    store_factory: Optional[Callable[[], "PipelineStore"]] = None,
    state_path: Path | str | None = None,
) -> None:
    """Runs forever as a background job on the current event loop,
    incrementally syncing `root` every `interval_seconds` (firing once
    immediately at startup, then on the interval) until interrupted
    (Ctrl-C / SIGINT).
    """
    collector = LocalFileCollector(root=root, state_path=state_path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    # model=None lets get_extractor() pick the active provider's own
    # default model instead of always defaulting to an Anthropic model id.
    extractor = get_extractor(model=model)
    tick = build_tick(collector, registry, chunker, extractor, store_factory=store_factory)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(tick, "interval", seconds=interval_seconds, next_run_time=datetime.now())
    scheduler.start()
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown()
        logger.info("scheduler stopped")
