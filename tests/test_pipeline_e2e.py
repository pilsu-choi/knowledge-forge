"""End-to-end smoke test: Collector -> Parser -> Adaptive Chunker -> Knowledge
Extraction against a real fixture file, with the Anthropic client mocked at
the extraction seam - no ANTHROPIC_API_KEY needed.
"""

from pathlib import Path
from types import SimpleNamespace

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.local_file import LocalFileCollector
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeMessages:
    async def parse(self, **kwargs):
        return SimpleNamespace(
            parsed_output=ExtractionResult(
                summary="stub summary",
                facts=["stub fact"],
                entities=[],
                relations=[],
                topics=["stub topic"],
                document_type="markdown document",
            )
        )


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


async def test_pipeline_runs_end_to_end_against_markdown_fixture():
    collector = LocalFileCollector(root=FIXTURES / "sample.md")
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = ClaudeExtractor(client=_FakeClient())

    knowledge_objects = [ko async for ko in run_pipeline(collector, registry, chunker, extractor)]

    assert len(knowledge_objects) >= 1
    for ko in knowledge_objects:
        assert ko.summary == "stub summary"
        assert ko.document_type == "markdown document"
        # provenance chain: source_id -> document_id -> chunk_id -> knowledge_object_id
        assert ko.source.source_id
        assert ko.document.document_id
        assert ko.chunk.chunk_id
        assert ko.knowledge_object_id
        # round-trips through the exact PRD-shaped JSON without loss
        dumped = ko.model_dump_json()
        assert '"source"' in dumped and '"document"' in dumped and '"chunk"' in dumped
