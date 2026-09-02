import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, ensure_utc
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS
from knowledge_forge.config import settings
from knowledge_forge.extraction.auth import credential_hint_ko, has_resolvable_credentials
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.okf import OKFWriter
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline
from knowledge_forge.scheduling.scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_TRIGGER_API_HOST,
    DEFAULT_TRIGGER_API_PORT,
)
from knowledge_forge.storage import PipelineStore

app = typer.Typer(help="Knowledge Forge: Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.")
db_app = typer.Typer(help="PostgreSQL storage schema management.")
app.add_typer(db_app, name="db")


def _ensure_authenticated() -> None:
    if not has_resolvable_credentials():
        typer.echo(credential_hint_ko(), err=True)
        raise typer.Exit(1)


def _build_collector(source: str, path: Optional[Path]) -> Collector:
    """`source` picks which Collector `extract`/`schedule` run: "local" (the
    default) walks `path` via LocalFileCollector; "slack"/"notion"/"gdrive"/
    "gmail" build an authenticated SaaS collector from KF_* env config via
    collectors/saas/factory.py's SAAS_COLLECTOR_BUILDERS (see
    wiki/saas-connectors.md) - `path` is unused for those. The same mapping
    backs api/routes/ingest.py's `source` field and worker.py's
    `{source}_ingest` job dispatch, so a source is picked the same way
    everywhere.
    """
    if source == "local":
        if path is None:
            typer.echo("--source local(기본값)은 PATH 인자가 필요합니다.", err=True)
            raise typer.Exit(1)
        return LocalFileCollector(root=path)

    builder = SAAS_COLLECTOR_BUILDERS.get(source)
    if builder is None:
        typer.echo(
            f"알 수 없는 --source '{source}' (local/{'/'.join(SAAS_COLLECTOR_BUILDERS)} 중 하나여야 합니다).",
            err=True,
        )
        raise typer.Exit(1)

    return builder()


def _parse_datetime_arg(value: str, option_name: str) -> datetime:
    """Parses `--since`/`--until`'s ISO 8601 string (`YYYY-MM-DD` or
    `YYYY-MM-DDTHH:MM:SS[+HH:MM]` - no trailing `Z`, Python's fromisoformat()
    doesn't accept it before 3.11 and this project supports 3.10+). A value
    with no offset is treated as UTC via `ensure_utc`, matching every
    Collector's UTC-aware `RawItem.modified_at`.
    """
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        typer.echo(
            f"{option_name}: '{value}'을(를) 날짜/시각으로 해석할 수 없습니다 "
            "(YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM:SS[+HH:MM] 형식을 사용하세요).",
            err=True,
        )
        raise typer.Exit(1)


def _open_store() -> Optional[PipelineStore]:
    """Builds a PipelineStore from KF_DATABASE_URL/KF_DATA_DIR, or None (with
    a warning) if no database is configured - storage stays opt-in so the
    simple `extract` flow keeps working without any Postgres setup."""
    if not settings.database_url:
        typer.echo(
            "KF_DATABASE_URL이 설정되지 않아 저장을 건너뜁니다 (출력만 생성).",
            err=True,
        )
        return None
    return PipelineStore(settings.database_url, settings.data_dir)


@app.callback()
def _main() -> None:
    """Keeps `extract` addressable as an explicit subcommand (`knowledge-forge extract ...`)
    even though it is currently the only command - Typer/Click collapse a
    single-command app to top-level args otherwise, which would silently
    break the documented `knowledge-forge extract <path>` invocation.
    """


@app.command()
def extract(
    path: Optional[Path] = typer.Argument(
        None, help="A local file or directory to ingest. Required when --source local (the default); unused otherwise."
    ),
    source: str = typer.Option(
        "local",
        "--source",
        help=f"Collector to run: local (default, needs PATH) or one of {', '.join(SAAS_COLLECTOR_BUILDERS)} "
        "(built from KF_* env config, see wiki/saas-connectors.md).",
    ),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: Optional[str] = typer.Option(
        None, "--model", help="Extraction model. Defaults to the active provider's (KF_LLM_PROVIDER) own default model."
    ),
    output: Optional[Path] = typer.Option(None, "--output", help="Output file. Defaults to stdout."),
    pretty: bool = typer.Option(False, "--pretty", help="Write an indented JSON array instead of JSONL."),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
    okf_dir: Optional[Path] = typer.Option(
        None,
        "--okf-dir",
        help="Also write a file-based OKF Knowledge Base tree (manifest.yaml + resources/objects/relations) to this directory, under a {project-id}/ subfolder.",
    ),
    project_id: str = typer.Option(
        "default", "--project-id", help="Tenancy key tagged on every Source/Document this run persists."
    ),
) -> None:
    """Run a local file/directory (or a SaaS source, via --source) through the full pipeline and emit Knowledge Objects."""
    _ensure_authenticated()
    collector = _build_collector(source, path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    extractor = get_extractor(model=model)
    pipeline_store = _open_store() if store else None
    okf_writer = OKFWriter(okf_dir, project_id=project_id) if okf_dir else None

    async def _run():
        if pipeline_store is not None:
            async with pipeline_store:
                await pipeline_store.init_schema()  # idempotent (CREATE TABLE IF NOT EXISTS)
                return [
                    outcome
                    async for outcome in run_pipeline(
                        collector,
                        registry,
                        chunker,
                        extractor,
                        store=pipeline_store,
                        okf_writer=okf_writer,
                        project_id=project_id,
                    )
                ]
        return [
            outcome
            async for outcome in run_pipeline(
                collector, registry, chunker, extractor, store=None, okf_writer=okf_writer, project_id=project_id
            )
        ]

    outcomes = asyncio.run(_run())
    if okf_writer is not None:
        okf_writer.write_manifest()
        okf_writer.write_views()

    knowledge_objects = [ko for outcome in outcomes for ko in outcome.objects]
    relations = [rel for outcome in outcomes for rel in outcome.relations]

    out = output.open("w", encoding="utf-8") if output else sys.stdout
    try:
        if pretty:
            payload = [ko.model_dump(mode="json") for ko in knowledge_objects]
            json.dump(payload, out, ensure_ascii=False, indent=2)
            out.write("\n")
        else:
            for ko in knowledge_objects:
                out.write(ko.model_dump_json())
                out.write("\n")
    finally:
        if output:
            out.close()

    typer.echo(
        f"{len(knowledge_objects)}개의 Knowledge Object, {len(relations)}개의 Relation 생성 완료"
        + (f" (OKF: {okf_dir})" if okf_dir else ""),
        err=True,
    )


@app.command()
def backfill(
    path: Optional[Path] = typer.Argument(
        None, help="A local file or directory to ingest. Required when --source local (the default); unused otherwise."
    ),
    since: str = typer.Option(
        ..., "--since", help="Start of the window to re-collect (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS[+HH:MM]; "
        "no tz offset is treated as UTC)."
    ),
    until: Optional[str] = typer.Option(
        None, "--until", help="End of the window (same format as --since). Defaults to now."
    ),
    source: str = typer.Option(
        "local",
        "--source",
        help=f"Collector to run: local (default, needs PATH) or one of {', '.join(SAAS_COLLECTOR_BUILDERS)} "
        "(built from KF_* env config, see wiki/saas-connectors.md).",
    ),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: Optional[str] = typer.Option(
        None, "--model", help="Extraction model. Defaults to the active provider's (KF_LLM_PROVIDER) own default model."
    ),
    output: Optional[Path] = typer.Option(None, "--output", help="Output file. Defaults to stdout."),
    pretty: bool = typer.Option(False, "--pretty", help="Write an indented JSON array instead of JSONL."),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
    okf_dir: Optional[Path] = typer.Option(
        None,
        "--okf-dir",
        help="Also write a file-based OKF Knowledge Base tree (manifest.yaml + resources/objects/relations) to this directory, under a {project-id}/ subfolder.",
    ),
    project_id: str = typer.Option(
        "default", "--project-id", help="Tenancy key tagged on every Source/Document this run persists."
    ),
) -> None:
    """Re-collect only items modified in [--since, --until] and run them through the full
    pipeline - for recovering data missed while `schedule`/`serve`'s scheduled sync wasn't
    running, without touching that source's incremental-sync cursor at all (see
    Collector.backfill_sync). Slack pushes the window down to `oldest`/`latest`; Gmail's
    `after:`/`before:` search only has day precision, so it widens to whole days and then
    filters each message's exact timestamp - --since/--until's time-of-day still applies
    precisely to what gets collected."""
    _ensure_authenticated()
    since_dt = _parse_datetime_arg(since, "--since")
    until_dt = _parse_datetime_arg(until, "--until") if until is not None else None
    if until_dt is not None and since_dt > until_dt:
        typer.echo("--since는 --until보다 이후일 수 없습니다.", err=True)
        raise typer.Exit(1)

    collector = _build_collector(source, path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    extractor = get_extractor(model=model)
    pipeline_store = _open_store() if store else None
    okf_writer = OKFWriter(okf_dir, project_id=project_id) if okf_dir else None

    async def _run():
        if pipeline_store is not None:
            async with pipeline_store:
                await pipeline_store.init_schema()  # idempotent (CREATE TABLE IF NOT EXISTS)
                return [
                    outcome
                    async for outcome in run_pipeline(
                        collector,
                        registry,
                        chunker,
                        extractor,
                        store=pipeline_store,
                        okf_writer=okf_writer,
                        project_id=project_id,
                        since=since_dt,
                        until=until_dt,
                    )
                ]
        return [
            outcome
            async for outcome in run_pipeline(
                collector,
                registry,
                chunker,
                extractor,
                store=None,
                okf_writer=okf_writer,
                project_id=project_id,
                since=since_dt,
                until=until_dt,
            )
        ]

    outcomes = asyncio.run(_run())
    if okf_writer is not None:
        okf_writer.write_manifest()
        okf_writer.write_views()

    knowledge_objects = [ko for outcome in outcomes for ko in outcome.objects]
    relations = [rel for outcome in outcomes for rel in outcome.relations]

    out = output.open("w", encoding="utf-8") if output else sys.stdout
    try:
        if pretty:
            payload = [ko.model_dump(mode="json") for ko in knowledge_objects]
            json.dump(payload, out, ensure_ascii=False, indent=2)
            out.write("\n")
        else:
            for ko in knowledge_objects:
                out.write(ko.model_dump_json())
                out.write("\n")
    finally:
        if output:
            out.close()

    typer.echo(
        f"백필 완료 ({since_dt.isoformat()} ~ {until_dt.isoformat() if until_dt else 'now'}): "
        f"{len(knowledge_objects)}개의 Knowledge Object, {len(relations)}개의 Relation 생성"
        + (f" (OKF: {okf_dir})" if okf_dir else ""),
        err=True,
    )


@app.command()
def schedule(
    path: Optional[Path] = typer.Argument(
        None, help="A local file or directory to ingest. Required when --source local (the default); unused otherwise."
    ),
    source: str = typer.Option(
        "local",
        "--source",
        help=f"Collector to run: local (default, needs PATH) or one of {', '.join(SAAS_COLLECTOR_BUILDERS)} "
        "(built from KF_* env config, see wiki/saas-connectors.md).",
    ),
    interval_seconds: int = typer.Option(
        DEFAULT_INTERVAL_SECONDS, "--interval-seconds", help="Seconds between incremental sync runs (default: 10 minutes)."
    ),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: Optional[str] = typer.Option(
        None, "--model", help="Extraction model. Defaults to the active provider's (KF_LLM_PROVIDER) own default model."
    ),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
    okf_dir: Optional[Path] = typer.Option(
        None,
        "--okf-dir",
        help="Also write a file-based OKF Knowledge Base tree (manifest.yaml + resources/objects/relations) to "
        "this directory, under {project-id}/ subfolder(s) - refreshed at the end of every tick, not just once.",
    ),
    project_id: str = typer.Option(
        "default", "--project-id", help="Tenancy key tagged on every Source/Document this run persists."
    ),
    trigger_api: bool = typer.Option(
        True,
        "--trigger-api/--no-trigger-api",
        help="Serve POST /trigger (and GET /health) for HTTP-based manual sync triggering, alongside SIGUSR1.",
    ),
    trigger_host: str = typer.Option(
        DEFAULT_TRIGGER_API_HOST, "--trigger-host", help="Bind address for the manual-trigger HTTP API."
    ),
    trigger_port: int = typer.Option(
        DEFAULT_TRIGGER_API_PORT, "--trigger-port", help="Bind port for the manual-trigger HTTP API."
    ),
) -> None:
    """Run the pipeline on a repeating interval inside one long-lived process,
    incrementally re-syncing `path` (or a SaaS source, via --source) each
    time (Ctrl-C to stop). An operator can force an out-of-cycle sync at any
    time, either via `kill -USR1 <pid>` or
    `POST http://<trigger-host>:<trigger-port>/trigger`."""
    _ensure_authenticated()

    # Local keeps going through run_scheduled_sync's own root+state_path
    # handling (disk-persisted cursor across process restarts, see
    # scheduler.py) - only SaaS sources are built up front and handed in as
    # an already-constructed collector, since they track their own in-memory
    # cursor across ticks within this one long-lived process.
    root = None
    collector = None
    if source == "local":
        if path is None:
            typer.echo("--source local(기본값)은 PATH 인자가 필요합니다.", err=True)
            raise typer.Exit(1)
        root = path
    else:
        collector = _build_collector(source, path)

    store_factory = None
    if store:
        if settings.database_url:
            store_factory = lambda: PipelineStore(settings.database_url, settings.data_dir)  # noqa: E731
        else:
            typer.echo(
                "KF_DATABASE_URL이 설정되지 않아 저장을 건너뜁니다 (출력만 생성).",
                err=True,
            )

    trigger_msg = (
        f", POST http://{trigger_host}:{trigger_port}/trigger로 즉시 동기화 가능" if trigger_api else ""
    )
    typer.echo(
        f"스케줄러 시작: {root if collector is None else source} 를 {interval_seconds}초 간격으로 증분 동기화합니다 "
        f"(중단하려면 Ctrl-C, 즉시 동기화하려면 다른 터미널에서 kill -USR1 <pid>{trigger_msg}).",
        err=True,
    )
    from knowledge_forge.scheduling.scheduler import run_scheduled_sync

    okf_writer = OKFWriter(okf_dir, project_id=project_id) if okf_dir else None

    asyncio.run(
        run_scheduled_sync(
            root=root,
            collector=collector,
            interval_seconds=interval_seconds,
            max_tokens=max_tokens,
            model=model,
            store_factory=store_factory,
            project_id=project_id,
            okf_writer=okf_writer,
            trigger_api_host=trigger_host if trigger_api else None,
            trigger_api_port=trigger_port if trigger_api else None,
        )
    )


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for the API server."),
    port: int = typer.Option(8000, "--port", help="Bind port for the API server."),
) -> None:
    """Run the FastAPI ingest/read server (see wiki/api.md). `POST /ingest`
    enqueues a job and returns immediately - `knowledge-forge worker`
    (a separate process) does the actual pipeline work."""
    # No _ensure_authenticated() here: unlike extract/schedule/worker, this
    # process never calls the LLM itself (it only enqueues jobs and reads
    # from Postgres) - requiring Anthropic credentials just to accept
    # ingest requests would be wrong.
    import uvicorn

    uvicorn.run("knowledge_forge.api.app:create_app", factory=True, host=host, port=port)


@app.command()
def worker(
    poll_interval_seconds: float = typer.Option(2.0, "--poll-interval-seconds", help="Delay between empty polls of the job queue."),
) -> None:
    """Run the job-queue worker: polls `jobs` (populated by `POST /ingest`)
    and runs the pipeline for each claimed job (see wiki/api.md). A
    long-running process, deployed separately from `serve` (Ctrl-C to stop)."""
    _ensure_authenticated()
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다 - worker는 job queue가 필수입니다.", err=True)
        raise typer.Exit(1)

    from knowledge_forge.worker import run_worker

    asyncio.run(
        run_worker(
            database_url=settings.database_url,
            data_dir=settings.data_dir,
            poll_interval_seconds=poll_interval_seconds,
        )
    )


@db_app.command("init")
def db_init() -> None:
    """Create the PostgreSQL schema (sources/documents/chunks/knowledge_objects/relations)."""
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)

    async def _run():
        async with PipelineStore(settings.database_url, settings.data_dir) as store:
            await store.init_schema()

    asyncio.run(_run())
    typer.echo("스키마 초기화 완료", err=True)


if __name__ == "__main__":
    app()
