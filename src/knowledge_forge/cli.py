import json
import sys
from pathlib import Path
from typing import Optional

import typer

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction import ClaudeExtractor
from knowledge_forge.extraction.claude_extractor import DEFAULT_MODEL
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline

app = typer.Typer(help="Knowledge Forge: Collector -> Parser -> Adaptive Chunker -> Knowledge Extraction.")


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
) -> None:
    """Run a local file/directory through the full pipeline and emit Knowledge Objects."""
    collector = LocalFileCollector(root=path)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=max_tokens)
    extractor = ClaudeExtractor(model=model)

    knowledge_objects = list(run_pipeline(collector, registry, chunker, extractor))

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


if __name__ == "__main__":
    app()
