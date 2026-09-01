from datetime import datetime, timezone

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import content_hash
from knowledge_forge.models.document import Heading, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.conversation import ConversationParser


async def test_parses_slack_episode_into_heading_and_per_message_textblocks():
    data = "A: 배포 언제 할까요?\n\nB: 오늘 오후 가능합니다".encode("utf-8")
    item = RawItem(
        uri="slack://general/episode/100.0",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
        metadata={"channel": "general", "thread_ts": None},
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.SLACK, uri=item.uri)

    document = await ConversationParser().parse(item, source_ref)

    assert isinstance(document.elements[0], Heading)
    assert document.elements[0].text == "Slack #general 대화"
    text_blocks = [e for e in document.elements if isinstance(e, TextBlock)]
    assert len(text_blocks) == 2
    assert text_blocks[0].text == "A: 배포 언제 할까요?"
    assert text_blocks[1].text == "B: 오늘 오후 가능합니다"


async def test_parses_gmail_thread_with_subject_heading():
    data = "From: a@example.com\nhello".encode("utf-8")
    item = RawItem(
        uri="gmail://thread/t1",
        content=data,
        content_hash=content_hash(data),
        modified_at=datetime.now(timezone.utc),
        metadata={"subject": "Deploy schedule"},
    )
    source_ref = SourceRef(source_id="s1", source_type=SourceType.GMAIL, uri=item.uri)

    document = await ConversationParser().parse(item, source_ref)

    assert document.elements[0].text == "Gmail Thread: Deploy schedule"
    text_blocks = [e for e in document.elements if isinstance(e, TextBlock)]
    assert len(text_blocks) == 1
    assert "hello" in text_blocks[0].text
