"""`since` on run_pipeline() routes the item stream through
Collector.backfill_sync() instead of Collector.sync(cursor) - see
pipeline.py's docstring and collectors/base.py's Collector.backfill_sync.
"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, Cursor, RawItem
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionItem, ExtractionResult
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeMessages:
    async def parse(self, **kwargs):
        return SimpleNamespace(
            parsed_output=ExtractionResult(
                summary="stub summary",
                entities=[],
                relations=[],
                items=[ExtractionItem(type="fact", statement="stub fact")],
                topics=["stub topic"],
                document_type="markdown document",
            )
        )


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


class _RecordingCollector(Collector):
    """Records which sync method run_pipeline actually called, instead of
    caring about real API pagination (that's collectors/saas/*'s own tests)."""

    source_type = SourceType.LOCAL_FILE

    def __init__(self, content: bytes):
        self._content = content
        self.calls: list[str] = []

    def _item(self) -> RawItem:
        return RawItem(
            uri="file://fake.md",
            content=self._content,
            content_hash=compute_content_hash(self._content),
            modified_at=datetime.now(timezone.utc),
        )

    def initial_sync(self):
        self.calls.append("initial_sync")
        yield self._item()

    def incremental_sync(self, cursor: Cursor):
        self.calls.append("incremental_sync")
        yield self._item()

    def get_cursor(self) -> Cursor:
        return Cursor()

    def backfill_sync(self, since, until=None):
        self.calls.append("backfill_sync")
        yield self._item()


def _pipeline_stack(content: bytes):
    collector = _RecordingCollector(content)
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = ClaudeExtractor(client=_FakeClient())
    return collector, registry, chunker, extractor


async def test_since_routes_through_backfill_sync_instead_of_sync():
    content = (FIXTURES / "sample.md").read_bytes()
    collector, registry, chunker, extractor = _pipeline_stack(content)

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    outcomes = [
        outcome async for outcome in run_pipeline(collector, registry, chunker, extractor, since=since)
    ]

    assert collector.calls == ["backfill_sync"]
    assert len(outcomes) >= 1


async def test_no_since_routes_through_sync():
    content = (FIXTURES / "sample.md").read_bytes()
    collector, registry, chunker, extractor = _pipeline_stack(content)

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor)]

    assert collector.calls == ["initial_sync"]
    assert len(outcomes) >= 1
