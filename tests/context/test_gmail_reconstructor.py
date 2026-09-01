from datetime import datetime, timezone

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.context.gmail import GmailThreadReconstructor


def _message(msg_id, thread_id, text, minute, from_="a@example.com", subject="Deploy?"):
    return RawItem(
        uri=f"gmail://{msg_id}",
        content=text.encode("utf-8"),
        content_hash="irrelevant",
        modified_at=datetime(2026, 9, 1, 10, minute, tzinfo=timezone.utc),
        metadata={
            "message_id": msg_id,
            "thread_id": thread_id,
            "subject": subject,
            "from": from_,
            "date": f"2026-09-01T10:{minute:02d}:00Z",
        },
    )


def test_groups_by_thread_and_sorts_chronologically():
    items = [
        _message("m2", "t1", "second reply", minute=5, from_="b@example.com"),
        _message("m1", "t1", "first message", minute=0, from_="a@example.com"),
        _message("m3", "t2", "unrelated thread", minute=1, from_="c@example.com"),
    ]

    merged = list(GmailThreadReconstructor().reconstruct(iter(items)))

    assert len(merged) == 2
    thread1 = next(m for m in merged if m.metadata["thread_id"] == "t1")
    assert thread1.uri == "gmail://thread/t1"
    assert thread1.metadata["message_ids"] == ["m1", "m2"]
    assert thread1.metadata["subject"] == "Deploy?"
    assert thread1.metadata["participants"] == ["a@example.com", "b@example.com"]
    text = thread1.content.decode("utf-8")
    assert text.index("first message") < text.index("second reply")
    assert thread1.modified_at == datetime(2026, 9, 1, 10, 5, tzinfo=timezone.utc)


def test_deleted_items_pass_through_unmerged():
    deleted = RawItem(
        uri="gmail://m9",
        content=b"",
        content_hash="",
        modified_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        metadata={"message_id": "m9"},
        deleted=True,
    )
    live = _message("m1", "t1", "hello", minute=0)

    merged = list(GmailThreadReconstructor().reconstruct(iter([deleted, live])))

    assert deleted in merged
    assert any(m.uri == "gmail://thread/t1" for m in merged)
