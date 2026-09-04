import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import typer

from knowledge_forge.agent.auth import agent_credential_hint_ko, has_resolvable_agent_credentials
from knowledge_forge.agent.providers.factory import get_chat_provider
from knowledge_forge.agent.runtime import AgentSession, run_query
from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, ensure_utc
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS
from knowledge_forge.config import settings
from knowledge_forge.embedding.factory import get_embedding_provider
from knowledge_forge.evaluation.judge import judge_search_log
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
from knowledge_forge.storage.eval_store import EvalStore
from knowledge_forge.storage.postgres_store import PostgresStore

app = typer.Typer(help="Knowledge Forge: Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.")
db_app = typer.Typer(help="PostgreSQL storage schema management.")
app.add_typer(db_app, name="db")


def _ensure_authenticated() -> None:
    if not has_resolvable_credentials():
        typer.echo(credential_hint_ko(), err=True)
        raise typer.Exit(1)


def _build_collector(source: str, path: Path | None) -> Collector:
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
        raise typer.Exit(1) from None


def _open_store() -> PipelineStore | None:
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
    path: Path | None = typer.Argument(
        None, help="A local file or directory to ingest. Required when --source local (the default); unused otherwise."
    ),
    source: str = typer.Option(
        "local",
        "--source",
        help=f"Collector to run: local (default, needs PATH) or one of {', '.join(SAAS_COLLECTOR_BUILDERS)} "
        "(built from KF_* env config, see wiki/saas-connectors.md).",
    ),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: str | None = typer.Option(
        None, "--model", help="Extraction model. Defaults to the active provider's (KF_LLM_PROVIDER) own default model."
    ),
    output: Path | None = typer.Option(None, "--output", help="Output file. Defaults to stdout."),
    pretty: bool = typer.Option(False, "--pretty", help="Write an indented JSON array instead of JSONL."),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
    okf_dir: Path | None = typer.Option(
        None,
        "--okf-dir",
        help="Also write a file-based OKF Knowledge Base tree (manifest.yaml + resources/objects/relations) to this directory, under a {project-id}/ subfolder.",
    ),
    project_id: str = typer.Option(
        "default", "--project-id", help="Tenancy key tagged on every Source/Document this run persists."
    ),
    embed: bool = typer.Option(
        settings.embedding_enabled,
        "--embed/--no-embed",
        help="Embed into knowledge_object_embeddings right after storing (default from KF_EMBEDDING_ENABLED, see "
        "wiki/embeddings.md). What gets embedded is --embedding-source. Requires --store - ignored with a warning "
        "otherwise, since there is nowhere to persist embeddings without a database.",
    ),
    embedding_source: str = typer.Option(
        settings.embedding_source,
        "--embedding-source",
        help="What --embed embeds: 'knowledge_object' (the extracted KO summary/content/topics, after extraction) "
        "or 'chunk' (the raw chunk text, right after chunking - available without waiting for extraction). "
        "Default from KF_EMBEDDING_SOURCE.",
    ),
    extract_objects: bool = typer.Option(
        True,
        "--extract/--no-extract",
        help="Run LLM Knowledge Extraction (the default). --no-extract stops after chunking - no KnowledgeObjects "
        "are produced; pair it with --embed --embedding-source chunk to still make the documents "
        "vector-searchable (see wiki/embeddings.md).",
    ),
) -> None:
    """Run a local file/directory (or a SaaS source, via --source) through the full pipeline and emit Knowledge Objects."""
    if embedding_source not in ("knowledge_object", "chunk"):
        typer.echo(f"--embedding-source는 knowledge_object 또는 chunk여야 합니다: {embedding_source!r}", err=True)
        raise typer.Exit(2)
    if extract_objects:
        _ensure_authenticated()
    collector = _build_collector(source, path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    extractor = get_extractor(model=model) if extract_objects else None
    pipeline_store = _open_store() if store else None
    okf_writer = OKFWriter(okf_dir, project_id=project_id) if okf_dir else None
    if embed and pipeline_store is None:
        typer.echo("--embed는 --store(및 KF_DATABASE_URL)가 있어야 동작합니다 - 임베딩을 건너뜁니다.", err=True)
    embedder = get_embedding_provider() if embed and pipeline_store is not None else None
    if not extract_objects and embedder is not None and embedding_source != "chunk":
        typer.echo(
            "--no-extract에 --embedding-source knowledge_object를 함께 주면 임베딩할 KnowledgeObject가 없습니다 - "
            "--embedding-source chunk를 쓰세요.",
            err=True,
        )

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
                        embedder=embedder,
                        embedding_source=embedding_source,
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
    path: Path | None = typer.Argument(
        None, help="A local file or directory to ingest. Required when --source local (the default); unused otherwise."
    ),
    since: str = typer.Option(
        ..., "--since", help="Start of the window to re-collect (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS[+HH:MM]; "
        "no tz offset is treated as UTC)."
    ),
    until: str | None = typer.Option(
        None, "--until", help="End of the window (same format as --since). Defaults to now."
    ),
    source: str = typer.Option(
        "local",
        "--source",
        help=f"Collector to run: local (default, needs PATH) or one of {', '.join(SAAS_COLLECTOR_BUILDERS)} "
        "(built from KF_* env config, see wiki/saas-connectors.md).",
    ),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: str | None = typer.Option(
        None, "--model", help="Extraction model. Defaults to the active provider's (KF_LLM_PROVIDER) own default model."
    ),
    output: Path | None = typer.Option(None, "--output", help="Output file. Defaults to stdout."),
    pretty: bool = typer.Option(False, "--pretty", help="Write an indented JSON array instead of JSONL."),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
    okf_dir: Path | None = typer.Option(
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
    path: Path | None = typer.Argument(
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
    model: str | None = typer.Option(
        None, "--model", help="Extraction model. Defaults to the active provider's (KF_LLM_PROVIDER) own default model."
    ),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
    okf_dir: Path | None = typer.Option(
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
            trigger_api_host=trigger_host,
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
    concurrency: int = typer.Option(4, "--concurrency", help="Number of concurrent Celery worker processes."),
    loglevel: str = typer.Option("info", "--loglevel", help="Celery worker log level."),
) -> None:
    """Run the Celery worker that consumes `jobs` (populated by `POST /ingest`,
    dispatched over Redis) and runs the pipeline for each job (see
    wiki/api.md). A long-running process, deployed separately from `serve`
    (Ctrl-C to stop). Monitor it with Flower (`celery -A
    knowledge_forge.celery_app flower`, or the `flower` docker-compose
    service)."""
    _ensure_authenticated()
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다 - worker는 job queue가 필수입니다.", err=True)
        raise typer.Exit(1)

    from knowledge_forge.celery_app import celery_app

    celery_app.worker_main(["worker", f"--loglevel={loglevel}", f"--concurrency={concurrency}"])


@app.command()
def embed(
    project_id: str | None = typer.Option(
        None, "--project-id", help="Only embed this project's KnowledgeObjects. Omit to scan every project."
    ),
    batch_size: int = typer.Option(64, "--batch-size", help="KnowledgeObjects/chunks fetched/embedded per API call."),
    model: str | None = typer.Option(
        None, "--model", help="Embedding model. Defaults to KF_EMBEDDING_MODEL (text-embedding-3-small)."
    ),
    embedding_source: str = typer.Option(
        settings.embedding_source,
        "--embedding-source",
        help="Which grain to backfill: 'knowledge_object' (KOs with no embedding yet) or 'chunk' (chunks with "
        "no embedding yet). Default from KF_EMBEDDING_SOURCE.",
    ),
) -> None:
    """Backfill embeddings for KnowledgeObjects (or, with --embedding-source chunk, raw chunks) already
    in Postgres that have none yet for the active embedding model (see wiki/embeddings.md) - e.g. after
    turning on KF_EMBEDDING_ENABLED for the first time, or after `extract --no-embed` runs. Does NOT
    re-embed rows that already have an embedding for this model, even if their text has since changed
    (see wiki/embeddings.md's staleness section) - only fills in what's missing."""
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다 - embed는 Postgres가 필수입니다.", err=True)
        raise typer.Exit(1)
    if embedding_source not in ("knowledge_object", "chunk"):
        typer.echo(f"--embedding-source는 knowledge_object 또는 chunk여야 합니다: {embedding_source!r}", err=True)
        raise typer.Exit(2)
    database_url = settings.database_url

    embedding_model = model or settings.embedding_model
    embedder = get_embedding_provider(model=embedding_model)

    async def _run() -> int:
        total = 0
        async with PipelineStore(database_url, settings.data_dir) as store:
            await store.init_schema()
            while True:
                if embedding_source == "chunk":
                    pairs = await store.list_chunks_missing_embedding(
                        embedding_model, project_id=project_id, limit=batch_size
                    )
                    if not pairs:
                        break
                    # One backfill batch can straddle projects (project_id
                    # filter omitted) - group so each record gets its own.
                    by_project: dict[str, list] = {}
                    for chunk, chunk_project in pairs:
                        by_project.setdefault(chunk_project, []).append(chunk)
                    for chunk_project, chunks in by_project.items():
                        await store.record_chunk_embeddings(chunks, chunk_project, embedder, embedding_model)
                    total += len(pairs)
                else:
                    batch = await store.list_knowledge_objects_missing_embedding(
                        embedding_model, project_id=project_id, limit=batch_size
                    )
                    if not batch:
                        break
                    await store.record_embeddings(batch, embedder, embedding_model)
                    total += len(batch)
                typer.echo(f"{total}개 임베딩 완료...", err=True)
        return total

    total = asyncio.run(_run())
    unit = "청크" if embedding_source == "chunk" else "KnowledgeObject"
    typer.echo(f"{total}개의 {unit} 임베딩 완료 (모델: {embedding_model})", err=True)


@app.command()
def agent(
    question: str | None = typer.Argument(
        None, help="One-shot question to ask the agent. Omit and pass --interactive for a REPL instead."
    ),
    project_id: str | None = typer.Option(
        None, "--project-id", help="Default project_id hint for file_search (the agent can still search other projects)."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="anthropic (default) or openai_compatible. Defaults to KF_AGENT_PROVIDER."
    ),
    model: str | None = typer.Option(None, "--model", help="Overrides the active provider's default model."),
    interactive: bool = typer.Option(False, "--interactive", help="REPL mode instead of a single one-shot question."),
) -> None:
    """OKF 지식 베이스를 file_search로 검색해 답하는 에이전트(wiki/agent-runtime.md) - 1회성
    쿼리든 --interactive든 동일한 벤더 중립 harness(agent/loop.py) 위에서 돈다. 두
    --provider(anthropic/openai_compatible) 모두 동일하게 동작한다."""
    if not settings.okf_dir:
        typer.echo("KF_OKF_DIR이 설정되지 않았습니다 - agent는 OKF 파일 트리가 필수입니다.", err=True)
        raise typer.Exit(1)
    if not interactive and not question:
        typer.echo("질문을 인자로 주거나 --interactive를 쓰세요.", err=True)
        raise typer.Exit(1)
    if not has_resolvable_agent_credentials():
        typer.echo(agent_credential_hint_ko(), err=True)
        raise typer.Exit(1)

    if interactive:
        session = AgentSession(project_id=project_id, provider=provider, model=model)

        async def _repl() -> None:
            typer.echo("knowledge-forge agent - 종료하려면 Ctrl+D", err=True)
            while True:
                try:
                    line = input("> ")
                except EOFError:
                    typer.echo("", err=True)
                    break
                if not line.strip():
                    continue
                if line.startswith("/feedback"):
                    _handle_feedback_command(line, session.last_log_id)
                    continue
                answer = await session.ask(line)
                typer.echo(answer)
                if session.last_log_id:
                    typer.echo(f"[log_id={session.last_log_id} - /feedback up|down [코멘트]로 평가 가능]", err=True)
                if session.plan.steps:
                    typer.echo(f"\n[계획]\n{session.plan.render()}\n", err=True)

        asyncio.run(_repl())
        return

    assert question is not None  # guaranteed above: not interactive and not question already exits
    answer, log_id = asyncio.run(run_query(question, project_id=project_id, provider=provider, model=model))
    typer.echo(answer)
    if log_id:
        typer.echo(f"[log_id={log_id} - `knowledge-forge feedback {log_id} up|down`로 평가 가능]", err=True)


def _handle_feedback_command(line: str, log_id: str | None) -> None:
    """`--interactive` REPL의 `/feedback up|down [코멘트]` - 직전 턴의
    `session.last_log_id`에 피드백을 붙인다 (wiki/search-evaluation.md (A))."""
    if log_id is None:
        typer.echo("아직 기록된 답변이 없거나 검색 로깅이 꺼져 있습니다(KF_DATABASE_URL 필요).", err=True)
        return
    parts = line.split(maxsplit=2)
    if len(parts) < 2 or parts[1] not in ("up", "down"):
        typer.echo("사용법: /feedback up|down [코멘트]", err=True)
        return
    rating = parts[1]
    comment = parts[2] if len(parts) > 2 else None
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        return
    database_url = settings.database_url

    async def _run() -> None:
        async with EvalStore(database_url) as store:
            await store.add_feedback(log_id, rating, comment)

    asyncio.run(_run())
    typer.echo(f"피드백 기록됨: {rating}", err=True)


@db_app.command("init")
def db_init() -> None:
    """Create the PostgreSQL schema (sources/documents/chunks/knowledge_objects/relations/
    search_log/search_feedback/eval_score/...) - the same schema.sql `agent`/`feedback`/`eval`
    read and write through `EvalStore`, so this must run before any of them against a fresh DB."""
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)

    async def _run():
        async with PipelineStore(settings.database_url, settings.data_dir) as store:
            await store.init_schema()

    asyncio.run(_run())
    typer.echo("스키마 초기화 완료", err=True)


@db_app.command("backfill-titles")
def db_backfill_titles(
    project_id: str | None = typer.Option(None, "--project-id", help="Only backfill this project's documents."),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Re-derive every title, not just NULL ones (after a change to the derivation rules)."
    ),
) -> None:
    """Fill `documents.title` for rows written before the column existed (see
    storage/document_title.py for the derivation order) so `document_search` can find
    them. New ingests fill it on write; this is only needed once per existing database
    (`db init` first - it adds the column)."""
    database_url = settings.database_url
    if not database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)

    async def _run() -> int:
        async with PipelineStore(database_url, settings.data_dir) as store:
            await store.init_schema()
            return await store.backfill_document_titles(project_id, overwrite=overwrite)

    updated = asyncio.run(_run())
    typer.echo(f"문서 제목 백필 완료: {updated}건", err=True)


@db_app.command("export-okf")
def db_export_okf(
    okf_dir: Path = typer.Option(..., "--okf-dir", help="Directory to write the OKF Knowledge Base tree to."),
    project_id: str | None = typer.Option(
        None, "--project-id", help="Only export this project's data. Omit to export every project found in Postgres."
    ),
    page_size: int = typer.Option(200, "--page-size", help="Rows fetched per paginated read."),
) -> None:
    """Rebuild a file-based OKF tree from Source/Document/KnowledgeObject/Relation data already
    persisted in Postgres - for a run that collected without --okf-dir. Resource-level Links
    (Slack thread sequencing, cross-source references) are NOT recoverable this way: pipeline.py
    only ever materializes them in memory during a live run and never persists them to Postgres,
    so this command's output has everything except links/."""
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)
    database_url = settings.database_url

    async def _run() -> tuple[int, int, int]:
        db = PostgresStore(database_url)
        await db.connect()
        try:
            writer = OKFWriter(okf_dir, project_id=project_id or "default")
            resource_count = object_count = relation_count = 0

            offset = 0
            while True:
                sources = await db.list_sources(project_id=project_id, limit=page_size, offset=offset)
                if not sources:
                    break
                for source in sources:
                    documents = await db.list_documents(source_id=source.source_id, limit=1)
                    if not documents:
                        continue
                    writer.write_resource(source, documents[0])
                    resource_count += 1

                    obj_offset = 0
                    while True:
                        objects = await db.list_knowledge_objects(
                            source_id=source.source_id, limit=page_size, offset=obj_offset
                        )
                        if not objects:
                            break
                        writer.write_objects(objects)
                        object_count += len(objects)
                        obj_offset += page_size
                offset += page_size

            # Relations carry no project_id/source of their own (see
            # OKFWriter.write_relations's docstring) - route each by the
            # project_id of the KnowledgeObject its subject_id names, same as
            # pipeline.py routes a live run's relations by the enclosing
            # document's source.project_id.
            subject_project: dict[str, str] = {}
            ko_offset = 0
            while True:
                objects = await db.list_knowledge_objects(limit=page_size, offset=ko_offset)
                if not objects:
                    break
                for ko in objects:
                    subject_project[ko.knowledge_object_id] = ko.source.project_id
                ko_offset += page_size

            rel_offset = 0
            while True:
                relations = await db.list_relations(limit=page_size, offset=rel_offset)
                if not relations:
                    break
                by_project: dict[str, list] = {}
                for rel in relations:
                    rel_project = subject_project.get(rel.subject_id) or subject_project.get(rel.object_id)
                    if project_id is not None and rel_project != project_id:
                        continue
                    by_project.setdefault(rel_project or "default", []).append(rel)
                for rel_project_id, rels in by_project.items():
                    writer.write_relations(rels, project_id=rel_project_id)
                    relation_count += len(rels)
                rel_offset += page_size

            writer.write_manifest()
            writer.write_views()
        finally:
            await db.close()
        return resource_count, object_count, relation_count

    resources, objects, relations = asyncio.run(_run())
    typer.echo(
        f"OKF 백필 완료: 리소스 {resources}개, Knowledge Object {objects}개, Relation {relations}개 (OKF: {okf_dir})",
        err=True,
    )


@app.command()
def feedback(
    log_id: str = typer.Argument(..., help="`knowledge-forge agent`/REPL이 답변과 함께 출력한 log_id."),
    rating: str = typer.Argument(..., help="'up' 또는 'down'."),
    comment: str | None = typer.Option(None, "--comment", help="선택적 코멘트."),
) -> None:
    """일회성 `knowledge-forge agent <질문>` 답변에 사후 피드백을 붙인다
    (wiki/search-evaluation.md (A)) - `--interactive` REPL은 대신 그 자리에서
    `/feedback up|down [코멘트]`를 쓸 수 있다."""
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다 - feedback은 Postgres가 필수입니다.", err=True)
        raise typer.Exit(1)
    database_url = settings.database_url
    if rating not in ("up", "down"):
        typer.echo("rating은 'up' 또는 'down'이어야 합니다.", err=True)
        raise typer.Exit(1)

    async def _run() -> bool:
        async with EvalStore(database_url) as store:
            if await store.get_search_log(log_id) is None:
                return False
            await store.add_feedback(log_id, rating, comment)
            return True

    if not asyncio.run(_run()):
        typer.echo(f"log_id '{log_id}'를 찾을 수 없습니다.", err=True)
        raise typer.Exit(1)
    typer.echo(f"피드백 기록됨: {rating}", err=True)


@app.command()
def eval(
    project_id: str | None = typer.Option(
        None, "--project-id", help="이 프로젝트의 search_log만 채점. 생략하면 모든 프로젝트."
    ),
    batch_size: int = typer.Option(20, "--batch-size", help="한 번에 가져와 채점할 search_log 행 수."),
    max_batches: int | None = typer.Option(
        None, "--max-batches", help="최대 몇 배치까지 돌지 제한(생략하면 미채점 행이 없어질 때까지)."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="채점에 쓸 ChatProvider. 생략하면 KF_AGENT_PROVIDER(에이전트와 동일)."
    ),
    model: str | None = typer.Option(None, "--model", help="채점 모델. 생략하면 그 provider의 기본 모델."),
) -> None:
    """아직 채점되지 않은 `search_log` 행을 LLM-judge로 채점해 `eval_score`에 저장한다
    (wiki/search-evaluation.md (B)) - Recall/Precision/MRR/NDCG(같은 문서 (C), 골드셋
    필요)와 달리 골드 relevance judgment 없이 지금 바로 돌릴 수 있다. 답변 생성 경로와
    분리된 오프라인 배치라 답변 지연시간에는 영향이 없다."""
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다 - eval은 Postgres가 필수입니다.", err=True)
        raise typer.Exit(1)
    database_url = settings.database_url

    chat_provider = get_chat_provider(provider=provider, model=model)
    # Mirrors get_chat_provider()'s own model resolution (agent/providers/factory.py)
    # - recorded on eval_score.judge_model for "which model actually judged this".
    active_provider = provider or settings.agent_provider
    default_model = settings.agent_openai_compatible_model if active_provider == "openai_compatible" else settings.agent_model
    judge_model = model or default_model

    async def _run() -> tuple[int, int]:
        scored = failed = 0
        async with EvalStore(database_url) as store:
            batches_run = 0
            while max_batches is None or batches_run < max_batches:
                logs = await store.list_unscored_search_logs(project_id=project_id, limit=batch_size)
                if not logs:
                    break
                for log in logs:
                    result = await judge_search_log(log, chat_provider)
                    if result is None:
                        failed += 1
                        continue
                    await store.save_eval_score(
                        log.log_id, result.groundedness_score, result.citation_precision, result.claims, judge_model
                    )
                    scored += 1
                batches_run += 1
        return scored, failed

    scored, failed = asyncio.run(_run())
    typer.echo(f"채점 완료: {scored}개 성공, {failed}개 실패(다음 실행에서 재시도)", err=True)


if __name__ == "__main__":
    app()
