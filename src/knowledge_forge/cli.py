import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import typer

from knowledge_forge.agent.auth import (
    OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO,
    _has_openai_compatible_credentials,
    agent_credential_hint_ko,
    has_resolvable_agent_credentials,
)
from knowledge_forge.agent.providers.base import ChatProvider
from knowledge_forge.agent.providers.factory import get_chat_provider
from knowledge_forge.agent.runtime import AgentSession, run_query
from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, ensure_utc
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.collectors.replay import RawBlobReplayCollector, filter_rows_by_extension
from knowledge_forge.collectors.saas.factory import SAAS_COLLECTOR_BUILDERS
from knowledge_forge.config import settings
from knowledge_forge.embedding.factory import get_embedding_provider
from knowledge_forge.eval import DEFAULT_MODEL, DEFAULT_PROVIDER
from knowledge_forge.eval.compare import compare_runs
from knowledge_forge.eval.generate import generate_golden, target_mix
from knowledge_forge.eval.goldenset import load_golden, save_golden
from knowledge_forge.eval.run import run_eval
from knowledge_forge.exceptions import EvalError
from knowledge_forge.extraction.auth import credential_hint_ko, has_resolvable_credentials
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.models.source import SourceType
from knowledge_forge.okf import OKFWriter
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline
from knowledge_forge.scheduling.scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_TRIGGER_API_HOST,
    DEFAULT_TRIGGER_API_PORT,
)
from knowledge_forge.storage import PipelineStore
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.storage.replay_index import load_replay_rows

app = typer.Typer(help="Knowledge Forge: Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.")
db_app = typer.Typer(help="PostgreSQL storage schema management.")
app.add_typer(db_app, name="db")
eval_app = typer.Typer(help="검색 평가: grain 고정 골든셋 생성/실행/비교 (wiki/evaluation.md).")
app.add_typer(eval_app, name="eval")


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
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens, min_tokens=settings.min_tokens_per_chunk)
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
def reparse(
    source_type: str = typer.Option(
        ...,
        "--source-type",
        help=f"Which already-collected sources to replay: one of {', '.join(t.value for t in SourceType)}.",
    ),
    project_id: str | None = typer.Option(
        None, "--project-id", help="Only replay sources tagged with this project_id. Default: every project."
    ),
    only_missing: bool = typer.Option(
        True,
        "--only-missing/--all",
        help="Only replay sources that never produced a documents row (the default - the recovery case, and what "
        "makes the run resumable). --all re-parses every selected source, e.g. after a parser's output changed.",
    ),
    extensions: str | None = typer.Option(
        None,
        "--extensions",
        help="Comma-separated allowlist of file extensions to replay (e.g. '.md,.xlsx'). Default: no filter. Use it "
        "to hold back formats whose parser needs credentials/budget you do not want to spend on this run.",
    ),
    uri: list[str] = typer.Option(
        [],
        "--uri",
        help="Replay exactly these sources.uri values (repeatable). Pair it with --all to re-parse a single file "
        "that already has a document, instead of re-parsing every sibling of its format to reach it.",
    ),
    exclude_uri: list[str] = typer.Option(
        [],
        "--exclude-uri",
        help="Hold these sources.uri values out of the batch (repeatable) - for landing one oversized or risky "
        "file separately from the rest of its format, so its effect on the corpus can be measured on its own.",
    ),
    limit: int | None = typer.Option(None, "--limit", help="Replay at most this many sources."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the selection (count per extension) and exit without parsing anything."
    ),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: str | None = typer.Option(
        None, "--model", help="Extraction model. Defaults to the active provider's (KF_LLM_PROVIDER) own default model."
    ),
    extract_objects: bool = typer.Option(
        False,
        "--extract/--no-extract",
        help="Run LLM Knowledge Extraction as well. Off by default here (unlike `extract`): a replay's job is to "
        "produce the Document/Chunk rows a past run failed to produce, and re-running extraction over a large "
        "recovered backlog is a separate, far more expensive decision.",
    ),
    embed: bool = typer.Option(
        False,
        "--embed/--no-embed",
        help="Embed after storing. Off by default here for the same reason as --extract: embedding a recovered "
        "backlog is a paid, separately-decided step (see wiki/embeddings.md).",
    ),
    embedding_source: str = typer.Option(
        settings.embedding_source, "--embedding-source", help="What --embed embeds: 'knowledge_object' or 'chunk'."
    ),
) -> None:
    """Re-parse sources whose raw bytes were already collected, without re-collecting them.

    Reads `sources.raw_path` blobs straight off disk (KF_DATA_DIR) and runs them
    back through Parser -> Chunker, so a source that was collected while its
    parser was missing or broken becomes a Document without any network call to
    the origin SaaS API. See collectors/replay.py.
    """
    if embedding_source not in ("knowledge_object", "chunk"):
        typer.echo(f"--embedding-source는 knowledge_object 또는 chunk여야 합니다: {embedding_source!r}", err=True)
        raise typer.Exit(2)
    try:
        resolved_source_type = SourceType(source_type)
    except ValueError:
        typer.echo(
            f"--source-type은 {', '.join(t.value for t in SourceType)} 중 하나여야 합니다: {source_type!r}", err=True
        )
        raise typer.Exit(2) from None
    database_url = settings.database_url
    if not database_url:
        typer.echo("KF_DATABASE_URL이 필요합니다 - reparse는 sources 테이블에서 재적재 대상을 고릅니다.", err=True)
        raise typer.Exit(1)
    if extract_objects:
        _ensure_authenticated()

    allowed_extensions = {e.strip().lower() for e in extensions.split(",") if e.strip()} if extensions else None
    pipeline_store = PipelineStore(database_url, settings.data_dir)
    embedder = get_embedding_provider() if embed else None

    async def _run(dsn: str) -> tuple[int, int, int]:
        rows = await load_replay_rows(
            dsn,
            resolved_source_type,
            project_id=project_id,
            only_missing_documents=only_missing,
            uris=list(uri) or None,
            exclude_uris=list(exclude_uri) or None,
        )
        if allowed_extensions is not None:
            rows = filter_rows_by_extension(rows, allowed_extensions)
        # Applied after the extension filter, not in the SQL: `--limit 10
        # --extensions .md` should mean ten markdown files, not "ten rows,
        # however many of them happen to be markdown".
        if limit is not None:
            rows = rows[:limit]

        by_extension: dict[str, int] = {}
        for row in rows:
            by_extension[row.extension or "(확장자 없음)"] = by_extension.get(row.extension or "(확장자 없음)", 0) + 1
        for extension, count in sorted(by_extension.items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {extension}: {count}", err=True)
        if dry_run or not rows:
            return len(rows), 0, 0

        collector = RawBlobReplayCollector(rows, resolved_source_type)
        extractor = get_extractor(model=model) if extract_objects else None
        async with pipeline_store:
            await pipeline_store.init_schema()  # idempotent (CREATE TABLE IF NOT EXISTS)
            documents = 0
            objects = 0
            async for outcome in run_pipeline(
                collector,
                default_registry(),
                AdaptiveChunker(max_tokens_per_chunk=max_tokens, min_tokens=settings.min_tokens_per_chunk),
                extractor,
                store=pipeline_store,
                embedder=embedder,
                embedding_source=embedding_source,
                force=True,
            ):
                documents += 1
                objects += len(outcome.objects)
        return len(rows), documents, objects

    selected, documents, objects = asyncio.run(_run(database_url))
    if dry_run:
        typer.echo(f"재적재 대상 {selected}건 (--dry-run, 실제 파싱하지 않음)", err=True)
        return
    typer.echo(
        f"재적재 대상 {selected}건 중 {documents}건 파이프라인 통과"
        + (f", Knowledge Object {objects}개 생성" if extract_objects else ""),
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
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens, min_tokens=settings.min_tokens_per_chunk)
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

    # Batches embedded at once - the embedding provider's own semaphore caps
    # in-flight requests at the same number (KF_MAX_CONCURRENT_LLM_REQUESTS).
    fan_out = settings.max_concurrent_llm_requests

    async def _run() -> int:
        total = 0
        async with PipelineStore(database_url, settings.data_dir) as store:
            await store.init_schema()
            while True:
                # One read fetches `fan_out` batches' worth of missing rows, split into disjoint batches and
                # embedded concurrently; the next read only happens once they are all written, so no row is
                # embedded twice.
                limit = batch_size * fan_out
                if embedding_source == "chunk":
                    pairs = await store.list_chunks_missing_embedding(embedding_model, project_id=project_id, limit=limit)
                    if not pairs:
                        break
                    writes = []
                    for start in range(0, len(pairs), batch_size):
                        # One backfill batch can straddle projects (project_id
                        # filter omitted) - group so each record gets its own.
                        by_project: dict[str, list] = {}
                        for chunk, chunk_project in pairs[start : start + batch_size]:
                            by_project.setdefault(chunk_project, []).append(chunk)
                        writes += [
                            store.record_chunk_embeddings(chunks, chunk_project, embedder, embedding_model)
                            for chunk_project, chunks in by_project.items()
                        ]
                    await asyncio.gather(*writes)
                    total += len(pairs)
                else:
                    objects = await store.list_knowledge_objects_missing_embedding(
                        embedding_model, project_id=project_id, limit=limit
                    )
                    if not objects:
                        break
                    await asyncio.gather(
                        *(
                            store.record_embeddings(objects[start : start + batch_size], embedder, embedding_model)
                            for start in range(0, len(objects), batch_size)
                        )
                    )
                    total += len(objects)
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
            try:
                while True:
                    try:
                        line = input("> ")
                    except EOFError:
                        typer.echo("", err=True)
                        break
                    if not line.strip():
                        continue
                    answer = await session.ask(line)
                    typer.echo(answer)
                    if session.plan.steps:
                        typer.echo(f"\n[계획]\n{session.plan.render()}\n", err=True)
            finally:
                await session.aclose()

        asyncio.run(_repl())
        return

    assert question is not None  # guaranteed above: not interactive and not question already exits
    answer = asyncio.run(run_query(question, project_id=project_id, provider=provider, model=model))
    typer.echo(answer)


@db_app.command("init")
def db_init() -> None:
    """Create the PostgreSQL schema (sources/documents/chunks/knowledge_objects/relations/...) -
    must run before any DB-backed command against a fresh DB."""
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


@db_app.command("normalize-unicode")
def db_normalize_unicode(
    project_id: str | None = typer.Option(
        None, "--project-id", help="이 프로젝트의 문서/청크만 처리한다 (기본: 전체 프로젝트)."
    ),
) -> None:
    """이미 저장된 `documents.title` / `chunks.text`를 NFC로 정규화한다.

    macOS·Google Drive를 거친 파일명은 한글 자모가 분해된 NFD로 들어와, NFC로 친 질의가
    `document_search`/`lexical_search`의 substring 단계에 구조적으로 못 맞는다(자세한 이유는
    `text_normalize.py`). 적재 경로는 이제 NFC로 저장하므로 이 명령은 그 이전에 쌓인 행을 한 번
    고치기 위한 것이고, 재실행하면 0건이 나오는 멱등 연산이다."""
    database_url = settings.database_url
    if not database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)

    async def _run() -> tuple[int, int]:
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await store.normalize_unicode_text(project_id)

    documents, chunks = asyncio.run(_run())
    typer.echo(f"유니코드 NFC 정규화 완료: 문서 제목 {documents}건, 청크 본문 {chunks}건", err=True)


@db_app.command("backfill-slack-names")
def db_backfill_slack_names(
    project_id: str | None = typer.Option(
        None, "--project-id", help="이 프로젝트의 슬랙 문서만 처리한다 (기본: 전체 프로젝트)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="바꿀 제목 예시만 보여주고 아무것도 쓰지 않는다."),
    refresh_first_message: bool = typer.Option(
        False,
        "--refresh-first-message",
        help="이미 채워진 sources.metadata.first_message도 원문에서 다시 유도한다. "
        "발췌 선정 규칙이 바뀌었을 때만 필요하다(평소엔 멱등이 맞다).",
    ),
) -> None:
    """이미 적재된 슬랙 문서의 채널 ID/유저 ID를 사람이 읽는 이름으로 풀어
    `sources.metadata`(`channel_name`/`participant_names`/`first_message`)에 채우고,
    `documents.title`을 다시 유도한다.

    왜: 슬랙 문서는 제목이 `#C0BGQEQG1L6 2026-09-01`이라 제목 조회가 구조적으로 못 맞는다.
    v0 골든셋 정답 문서 90건 중 48건이 슬랙인데, 제목 검색으로 닿는 슬랙 정답은 66건 중
    0건이었다(오프라인 실측). 새 제목은 `#<채널명> <날짜> <첫 메시지 발췌 120자>`다 -
    조각을 그렇게 고른 근거는 `storage/document_title.py` 주석.

    `sources.metadata`와 `documents.title` **두 컬럼만** 쓴다(chunks/embeddings 불변).
    원시 `channel`/`participants` ID는 지우지 않고 키만 덧붙이므로 재실행하면 0건이 나오는
    멱등 연산이다. Slack Web API(`conversations.info` 채널당 1회, `users.list` 전체 1회)를
    쓰므로 `KF_SLACK_BOT_TOKEN`이 필요하다."""
    database_url = settings.database_url
    if not database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)

    from knowledge_forge.collectors.saas.factory import build_slack_directory

    directory = build_slack_directory()

    async def _run() -> dict:
        async with PipelineStore(database_url, settings.data_dir) as store:
            if dry_run:
                return await _preview_slack_titles(store, directory, project_id, refresh_first_message)
            return await store.backfill_slack_display_names(directory, project_id, refresh_first_message)

    stats = asyncio.run(_run())
    if dry_run:
        for before, after in stats["samples"]:
            typer.echo(f"  {before}\n   -> {after}", err=True)
        typer.echo(f"바뀔 제목 {stats['titles_updated']}건 / 스캔 {stats['sources_scanned']}건 (dry-run)", err=True)
        return
    typer.echo(
        f"슬랙 이름 백필 완료: 소스 {stats['sources_scanned']}건 스캔, "
        f"채널명 {stats['channels_resolved']}건 해석(못 푼 채널 {stats['channels_unresolved']}개), "
        f"참여자 이름 {stats['participants_resolved']}건, 첫 메시지 {stats['first_messages_filled']}건, "
        f"메타데이터 {stats['sources_updated']}건 갱신, 제목 {stats['titles_updated']}건 갱신",
        err=True,
    )


async def _preview_slack_titles(
    store, directory, project_id: str | None, refresh_first_message: bool = False
) -> dict:
    """`--dry-run`용. 트랜잭션을 열어 실제 백필을 돌린 뒤 **롤백**한다 - 미리보기 전용으로
    제목 유도 로직을 한 벌 더 쓰면 두 벌이 서로 어긋나는 게 시간 문제라서, 진짜 경로를
    그대로 돌리고 되돌린다."""
    before: dict[str, str | None] = {}
    async with store.conn.cursor() as cur:
        await cur.execute(
            "SELECT d.document_id, d.title FROM documents d JOIN sources s ON s.source_id = d.source_id "
            "WHERE s.source_type = 'slack' AND (%s::text IS NULL OR d.project_id = %s)",
            (project_id, project_id),
        )
        before = {row[0]: row[1] for row in await cur.fetchall()}
    async with store.conn.transaction(force_rollback=True):
        stats = await store.backfill_slack_display_names(directory, project_id, refresh_first_message)
        async with store.conn.cursor() as cur:
            await cur.execute(
                "SELECT d.document_id, d.title FROM documents d JOIN sources s ON s.source_id = d.source_id "
                "WHERE s.source_type = 'slack' AND (%s::text IS NULL OR d.project_id = %s) LIMIT 200",
                (project_id, project_id),
            )
            samples = [
                (before.get(row[0]), row[1])
                for row in await cur.fetchall()
                if before.get(row[0]) != row[1]
            ][:10]
    stats["samples"] = samples
    return stats


@db_app.command("backfill-links")
def db_backfill_links(
    project_id: str | None = typer.Option(
        None, "--project-id", help="이 프로젝트의 문서만 처리한다 (기본: 전체 프로젝트)."
    ),
) -> None:
    """Parse version metadata (documents.metadata.version_*) and rebuild document_links
    (PREVIOUS_VERSION / THREAD_PARENT / same_channel_sequence / thread_sequence) for rows
    ingested before that stage existed - idempotent, safe to re-run (see document_links.py)."""
    database_url = settings.database_url
    if not database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)

    async def _run() -> dict:
        async with PipelineStore(database_url, settings.data_dir) as store:
            await store.init_schema()
            return await store.backfill_document_versions_and_links(project_id)

    stats = asyncio.run(_run())
    typer.echo(
        f"문서 {stats['documents_scanned']}건 스캔, 버전 family 부여 {stats['documents_versioned']}건"
        f"(명시적 버전 표기 {stats['documents_with_marker']}건), family {stats['families']}개"
        f"(2건 이상 {stats['families_multi']}개)",
        err=True,
    )
    for link_type, count in stats["links_by_type"].items():
        typer.echo(f"  {link_type}: {count}건", err=True)
    if not stats["links_by_type"]:
        typer.echo("  생성된 링크 없음", err=True)


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


if __name__ == "__main__":
    app()


def _eval_database_url() -> str:
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다 - eval은 Postgres가 필수입니다.", err=True)
        raise typer.Exit(1)
    return settings.database_url


def _eval_chat_provider(provider: str, model: str) -> ChatProvider:
    if provider == "openai_compatible" and not _has_openai_compatible_credentials():
        # .env의 KF_ 접두사 없는 키는 settings가 읽지 않는다 - 셸에 export해야 한다.
        typer.echo(OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO, err=True)
        raise typer.Exit(1)
    return get_chat_provider(provider=provider, model=model)


@eval_app.command("gen")
def eval_gen(
    grain: str = typer.Option(..., "--grain", help="chunk 또는 knowledge_object - 골든셋에 박혀 run이 따른다."),
    n: int = typer.Option(300, "--n", help="항목 수. lookup 70% / exhaustive 15% / none 15%."),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    provider: str = typer.Option(DEFAULT_PROVIDER, "--provider", help="anthropic 또는 openai_compatible."),
    out: Path | None = typer.Option(None, "--out", help="기본 tests/eval/golden/<version>.json."),
    seed: int = typer.Option(0, "--seed", help="샘플링 순서 시드."),
) -> None:
    """골든셋 생성 - 샘플링, LLM 생성, 규칙·라벨 확장·none 게이트. 산출은 status=draft, 사람이 표본을 보고 approved로."""
    if grain not in ("chunk", "knowledge_object"):
        typer.echo(f"--grain은 chunk 또는 knowledge_object여야 합니다: {grain!r}", err=True)
        raise typer.Exit(2)
    database_url = _eval_database_url()
    chat_provider = _eval_chat_provider(provider, model)

    async def _run() -> tuple:
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await generate_golden(store, chat_provider, grain=grain, n=n, seed=seed)  # type: ignore[arg-type]

    try:
        golden, rejects = asyncio.run(_run())
    except EvalError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    path = out or Path("tests/eval/golden") / f"{golden.version}.json"
    save_golden(golden, path)
    counts = Counter(item.kind for item in golden.items)
    target = target_mix(n)
    typer.echo(f"{path}: {len(golden.items)}개 (grain={grain})")
    for kind in ("lookup", "exhaustive", "none"):
        typer.echo(f"  {kind:<10} {counts[kind]:>4} / 목표 {target[kind]}")
    typer.echo("거부: " + (", ".join(f"{reason}={count}" for reason, count in rejects.most_common()) or "없음"))


@eval_app.command("run")
def eval_run(
    golden_path: Path = typer.Option(..., "--golden", help="골든셋 JSON."),
    split: str | None = typer.Option(None, "--split", help="dev 또는 test. 생략하면 전체."),
    e2e: bool = typer.Option(False, "--e2e", help="에이전트 답변까지 실행하고 judge로 채점."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="e2e 에이전트·judge / --llm-constraints 모델."),
    provider: str = typer.Option(DEFAULT_PROVIDER, "--provider"),
    concurrency: int = typer.Option(4, "--concurrency"),
    llm_constraints: bool = typer.Option(False, "--llm-constraints", help="retrieval에서 LLM 제약 보강 + multihop을 켠다(기본 꺼짐, 결정론)."),
    query_embeddings: Path | None = typer.Option(None, "--query-embeddings", help="질의 임베딩 고정 파일. 있으면 재생(없는 텍스트만 원격 호출·추가), 없으면 이번 실행 것을 기록."),
) -> None:
    """골든셋 실행 - 결과 전체는 out/eval/<run_id>.json, 화면에는 kind별 요약."""
    database_url = _eval_database_url()
    golden = load_golden(golden_path)
    use_llm = e2e or llm_constraints
    chat_provider = _eval_chat_provider(provider, model) if use_llm else None

    async def _run() -> dict:
        async with PipelineStore(database_url, settings.data_dir) as store:
            return await run_eval(
                golden,
                store,
                split=split,
                e2e=e2e,
                provider=chat_provider,
                llm_constraints=llm_constraints,
                concurrency=concurrency,
                query_embeddings=query_embeddings,
                models={"provider": provider, "model": model} if use_llm else {},
            )

    report = asyncio.run(_run())
    path = Path("out/eval") / f"{report['run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    typer.echo(f"{path}  (grain={golden.grain}, mode={report['mode']}, git={report['provenance']['git']})")
    for kind, stats in report["summary"].items():
        typer.echo(f"  {kind:<10} " + "  ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in stats.items()))
    stale = [f"{r['id']}({r['stale_reason']})" for r in report["items"] if r["status"] == "stale"]
    if stale:
        typer.echo("stale: " + ", ".join(stale), err=True)
    for warning in report["warnings"]:
        typer.echo(f"경고: {warning}", err=True)


@eval_app.command("compare")
def eval_compare(
    run_a: Path = typer.Argument(..., help="기준 실행 결과 JSON."),
    run_b: Path = typer.Argument(..., help="비교 실행 결과 JSON (delta = B - A)."),
    metric: str = typer.Option("ndcg@10", "--metric", help="hit@1, recall@5, mrr, ndcg@10, recall, precision, none_correct, score, answer_correct 등."),
) -> None:
    """질의별 paired delta + bootstrap 95% CI(시드 고정) + 승/패/무."""
    try:
        result = compare_runs(json.loads(run_a.read_text(encoding="utf-8")), json.loads(run_b.read_text(encoding="utf-8")), metric)
    except EvalError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    low, high = result["ci95"]
    typer.echo(
        f"{metric}: A {result['mean_a']:.4f} -> B {result['mean_b']:.4f}  delta {result['delta']:+.4f}  "
        f"95% CI [{low:+.4f}, {high:+.4f}]  n={result['n']}"
    )
    typer.echo(f"승 {result['wins']} / 패 {result['losses']} / 무 {result['ties']}")
    for change in result["changed"][:10]:
        typer.echo(f"  {change['id']:<16} {change['a']:.3f} -> {change['b']:.3f} ({change['delta']:+.3f})")
    for warning in result["warnings"]:
        typer.echo(f"경고: {warning}", err=True)
