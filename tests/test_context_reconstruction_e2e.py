"""End-to-end: a Slack collector's per-message RawItems get merged by
Context Reconstruction into fewer, semantically complete conversations
before Parser/Chunker/Extraction ever see them - see
wiki/roadmap.md's Source-aware Context Reconstruction section.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterator

import yaml

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.base import Collector, Cursor, RawItem
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionItem, ExtractionResult
from knowledge_forge.models.source import SourceType
from knowledge_forge.okf import OKFWriter
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


class _FakeGmailCollector(Collector):
    source_type = SourceType.GMAIL

    def initial_sync(self) -> Iterator[RawItem]:
        threads = [
            ("t1", "m1", "Deploy?", "a@example.com", 0),
            ("t2", "m2", "Unrelated topic", "b@example.com", 60),
        ]
        for thread_id, msg_id, subject, from_, minutes_offset in threads:
            yield RawItem(
                uri=f"gmail://{msg_id}",
                content=f"{subject} - body".encode("utf-8"),
                content_hash="irrelevant",
                modified_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes_offset),
                metadata={
                    "message_id": msg_id,
                    "thread_id": thread_id,
                    "subject": subject,
                    "from": from_,
                    "date": "2026-09-01T10:00:00Z",
                },
            )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        return iter([])

    def get_cursor(self) -> Cursor:
        return Cursor()


class _FakeMessages:
    async def parse(self, **kwargs):
        return SimpleNamespace(
            parsed_output=ExtractionResult(
                summary="stub summary",
                entities=[],
                relations=[],
                items=[ExtractionItem(type="fact", statement="stub fact")],
                topics=["stub topic"],
                document_type="slack conversation",
            )
        )


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


async def test_slack_messages_are_merged_into_fewer_knowledge_objects_by_conversation():
    collector = _FakeSlackCollector()
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = ClaudeExtractor(client=_FakeClient())

    outcomes = [outcome async for outcome in run_pipeline(collector, registry, chunker, extractor)]
    knowledge_objects = [ko for outcome in outcomes for ko in outcome.objects]

    # 4 raw messages, but only 2 semantic episodes (a 3-message conversation
    # + 1 unrelated later message split off by the time-gap threshold) -> 2
    # Knowledge Objects (one stub "fact" item per episode chunk), not 4.
    assert len(knowledge_objects) == 2
    for ko in knowledge_objects:
        assert ko.document_type == "slack conversation"


async def test_consecutive_slack_episodes_produce_a_same_channel_sequence_link(tmp_path):
    """wiki/okf.md's "그래프 뷰어 대비" decision: the 2 episodes above are
    separate resources Relation can't connect (it only links KnowledgeObjects
    within one document) - OKFWriter should end up with a links/*.yaml
    record joining them, resolvable purely by parsing the file tree."""
    collector = _FakeSlackCollector()
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = ClaudeExtractor(client=_FakeClient())
    writer = OKFWriter(tmp_path)

    outcomes = [
        outcome
        async for outcome in run_pipeline(collector, registry, chunker, extractor, okf_writer=writer)
    ]
    assert len(outcomes) == 2  # sanity: still 2 episodes, per the test above
    writer.write_manifest()

    manifest = yaml.safe_load((tmp_path / "default" / "manifest.yaml").read_text(encoding="utf-8"))
    assert len(manifest["links"]) == 1
    link_entry = manifest["links"][0]
    assert link_entry["type"] == "same_channel_sequence"

    link_payload = yaml.safe_load((tmp_path / "default" / link_entry["path"]).read_text(encoding="utf-8"))
    resource_files = sorted((tmp_path / "default" / "resources").glob("*.yaml"))
    assert len(resource_files) == 2
    resource_ids = {f.stem for f in resource_files}
    assert link_payload["source_ref"]["source_id"] in resource_ids
    assert link_payload["target_ref"]["source_id"] in resource_ids
    assert link_payload["source_ref"]["source_id"] != link_payload["target_ref"]["source_id"]


async def test_consecutive_gmail_threads_produce_a_thread_sequence_link(tmp_path):
    collector = _FakeGmailCollector()
    registry = default_registry()
    chunker = AdaptiveChunker(max_tokens_per_chunk=4000)
    extractor = ClaudeExtractor(client=_FakeClient())
    writer = OKFWriter(tmp_path)

    outcomes = [
        outcome
        async for outcome in run_pipeline(collector, registry, chunker, extractor, okf_writer=writer)
    ]
    assert len(outcomes) == 2  # 2 threads -> 2 documents
    writer.write_manifest()

    manifest = yaml.safe_load((tmp_path / "default" / "manifest.yaml").read_text(encoding="utf-8"))
    assert len(manifest["links"]) == 1
    assert manifest["links"][0]["type"] == "thread_sequence"
