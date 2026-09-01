import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

import typer

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.config import settings
from knowledge_forge.extraction import ClaudeExtractor
from knowledge_forge.extraction.auth import CREDENTIAL_HINT_KO, has_resolvable_credentials
from knowledge_forge.extraction.claude_extractor import DEFAULT_MODEL
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline
from knowledge_forge.storage import PipelineStore

app = typer.Typer(help="Knowledge Forge: Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.")
db_app = typer.Typer(help="PostgreSQL storage schema management.")
app.add_typer(db_app, name="db")


def _ensure_authenticated() -> None:
    if not has_resolvable_credentials():
        typer.echo(CREDENTIAL_HINT_KO, err=True)
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
    path: Path = typer.Argument(..., help="A local file or directory to ingest."),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Claude model used for Knowledge Extraction."),
    output: Optional[Path] = typer.Option(None, "--output", help="Output file. Defaults to stdout."),
    pretty: bool = typer.Option(False, "--pretty", help="Write an indented JSON array instead of JSONL."),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
) -> None:
    """Run a local file/directory through the full pipeline and emit Knowledge Objects."""
    _ensure_authenticated()
    collector = LocalFileCollector(root=path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    extractor = ClaudeExtractor(model=model)
    pipeline_store = _open_store() if store else None

    with pipeline_store or nullcontext():
        if pipeline_store is not None:
            pipeline_store.init_schema()  # idempotent (CREATE TABLE IF NOT EXISTS)
        knowledge_objects = list(
            run_pipeline(collector, registry, chunker, extractor, store=pipeline_store)
        )

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

    typer.echo(f"{len(knowledge_objects)}개의 Knowledge Object 생성 완료", err=True)


@app.command()
def schedule(
    path: Path = typer.Argument(..., help="A local file or directory to ingest."),
    interval_seconds: int = typer.Option(3600, "--interval-seconds", help="Seconds between incremental sync runs."),
    max_tokens: int = typer.Option(4000, "--max-tokens", help="Max tokens per chunk before splitting further."),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Claude model used for Knowledge Extraction."),
    store: bool = typer.Option(
        True, "--store/--no-store", help="Persist Source/Document/Chunk/KnowledgeObject to Postgres + raw bytes to disk."
    ),
) -> None:
    """Run the pipeline on a repeating interval inside one long-lived
    process, incrementally re-syncing `path` each time (Ctrl-C to stop)."""
    _ensure_authenticated()

    store_factory = None
    if store:
        if settings.database_url:
            store_factory = lambda: PipelineStore(settings.database_url, settings.data_dir)  # noqa: E731
        else:
            typer.echo(
                "KF_DATABASE_URL이 설정되지 않아 저장을 건너뜁니다 (출력만 생성).",
                err=True,
            )

    typer.echo(
        f"스케줄러 시작: {path} 를 {interval_seconds}초 간격으로 증분 동기화합니다 (중단하려면 Ctrl-C).",
        err=True,
    )
    from knowledge_forge.scheduling.scheduler import run_scheduled_sync

    run_scheduled_sync(
        root=path,
        interval_seconds=interval_seconds,
        max_tokens=max_tokens,
        model=model,
        store_factory=store_factory,
    )


@db_app.command("init")
def db_init() -> None:
    """Create the PostgreSQL schema (sources/documents/chunks/knowledge_objects)."""
    if not settings.database_url:
        typer.echo("KF_DATABASE_URL이 설정되지 않았습니다.", err=True)
        raise typer.Exit(1)
    with PipelineStore(settings.database_url, settings.data_dir) as store:
        store.init_schema()
    typer.echo("스키마 초기화 완료", err=True)


if __name__ == "__main__":
    app()
