"""End-to-end: a Slack collector's per-message RawItems get merged by
Context Reconstruction into fewer, semantically complete conversations
before Parser/Chunker/Extraction ever see them - see
wiki/roadmap.md's Source-aware Context Reconstruction section.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterator

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, Cursor, RawItem
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.models.source import SourceType
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline


class _FakeSlackCollector(Collector):
    source_type = SourceType.SLACK

    def initial_sync(self) -> Iterator[RawItem]:
        conversation = [
            ("100.0", "A", "배포 언제 할까요?"),
            ("160.0", "B", "오늘 오후 가능할 것 같습니다"),
            ("220.0", "A", "네 그럼 오후로 합시다"),
        ]
        for ts, user, text in conversation:
            yield RawItem(
                uri=f"slack://general/{ts}",
                content=text.encode("utf-8"),
                content_hash="irrelevant",
                modified_at=datetime.fromtimestamp(float(ts), tz=timezone.utc),
                metadata={"channel": "general", "ts": ts, "user": user, "thread_ts": None},
            )

        # A much later, unrelated message - should become its own episode.
        yield RawItem(
            uri="slack://general/100000.0",
            content="C: 완전히 다른 주제입니다".encode("utf-8"),
            content_hash="irrelevant",
            modified_at=datetime.fromtimestamp(100000.0, tz=timezone.utc),
            metadata={"channel": "general", "ts": "100000.0", "user": "C", "thread_ts": None},
        )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        return iter([])

    def get_cursor(self) -> Cursor:
        return Cursor()


class _FakeMessages:
    def parse(self, **kwargs):
        return SimpleNamespace(
            parsed_output=ExtractionResult(
                summary="stub summary",
                facts=["stub fact"],
                entities=[],
                relations=[],
                topics=["stub topic"],
                document_type="slack conversation",
            )
        )


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def test_slack_messages_are_merged_into_fewer_knowledge_objects_by_conversation():
    collector = _FakeSlackCollector()
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = ClaudeExtractor(client=_FakeClient())

    knowledge_objects = list(run_pipeline(collector, registry, chunker, extractor))

    # 4 raw messages, but only 2 semantic episodes (a 3-message conversation
    # + 1 unrelated later message split off by the time-gap threshold) -> 2
    # Knowledge Objects, not 4.
    assert len(knowledge_objects) == 2
    for ko in knowledge_objects:
        assert ko.document_type == "slack conversation"
