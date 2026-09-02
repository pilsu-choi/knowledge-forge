from datetime import datetime, timezone

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.context.slack import SlackConversationReconstructor


def _msg(channel, ts, user, text, thread_ts=None):
    return RawItem(
        uri=f"slack://{channel}/{ts}",
        content=text.encode("utf-8"),
        content_hash="irrelevant",
        modified_at=datetime.fromtimestamp(float(ts), tz=timezone.utc),
        metadata={"channel": channel, "ts": ts, "user": user, "thread_ts": thread_ts},
    )


def test_thread_replies_are_grouped_into_one_episode_regardless_of_gap():
    items = [
        _msg("general", "100.0", "A", "배포 언제 할까요?", thread_ts="100.0"),
        _msg("general", "5000.0", "B", "오늘 오후 가능합니다", thread_ts="100.0"),
    ]

    episodes = list(SlackConversationReconstructor(gap_seconds=60).reconstruct(iter(items)))

    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.uri == "slack://general/thread/100.0"
    assert episode.metadata["thread_ts"] == "100.0"
    assert episode.metadata["participants"] == ["A", "B"]
    text = episode.content.decode("utf-8")
    assert text.index("배포 언제") < text.index("오늘 오후")


def test_non_thread_messages_segment_on_time_gap():
    items = [
        _msg("general", "0.0", "A", "hello"),
        _msg("general", "60.0", "B", "hi back"),
        # gap of 10000s > gap_seconds=1800 -> new episode
        _msg("general", "10060.0", "A", "unrelated later topic"),
    ]

    episodes = list(SlackConversationReconstructor(gap_seconds=1800).reconstruct(iter(items)))

    assert len(episodes) == 2
    assert episodes[0].metadata["message_count"] == 2
    assert episodes[0].metadata["participants"] == ["A", "B"]
    assert episodes[1].metadata["message_count"] == 1
    assert "thread_ts" not in episodes[1].metadata


def test_consecutive_timeline_episodes_in_same_channel_are_linked():
    items = [
        _msg("general", "0.0", "A", "hello"),
        # gap of 10000s > gap_seconds=1800 -> new episode
        _msg("general", "10000.0", "A", "unrelated later topic"),
    ]

    episodes = list(SlackConversationReconstructor(gap_seconds=1800).reconstruct(iter(items)))

    assert len(episodes) == 2
    assert "links" not in episodes[0].metadata
    assert episodes[1].metadata["links"] == [
        {"type": "same_channel_sequence", "target_uri": episodes[0].uri}
    ]


def test_thread_episodes_are_not_linked_into_the_timeline_sequence():
    items = [
        _msg("general", "100.0", "A", "배포 언제 할까요?", thread_ts="100.0"),
        _msg("general", "5000.0", "B", "오늘 오후 가능합니다", thread_ts="100.0"),
        _msg("general", "10000.0", "A", "unrelated timeline message"),
    ]

    episodes = list(SlackConversationReconstructor(gap_seconds=60).reconstruct(iter(items)))

    thread_episode = next(e for e in episodes if e.metadata.get("thread_ts"))
    timeline_episode = next(e for e in episodes if not e.metadata.get("thread_ts"))
    assert "links" not in thread_episode.metadata
    assert "links" not in timeline_episode.metadata  # nothing preceded it in the timeline


def test_channels_are_segmented_independently():
    items = [
        _msg("general", "0.0", "A", "hello general"),
        _msg("random", "0.0", "B", "hello random"),
    ]

    episodes = list(SlackConversationReconstructor().reconstruct(iter(items)))

    channels = {e.metadata["channel"] for e in episodes}
    assert channels == {"general", "random"}


def test_deleted_items_pass_through_unmerged():
    deleted = RawItem(
        uri="slack://general/1.0",
        content=b"",
        content_hash="",
        modified_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        metadata={"channel": "general", "ts": "1.0"},
        deleted=True,
    )
    live = _msg("general", "2.0", "A", "hi")

    merged = list(SlackConversationReconstructor().reconstruct(iter([deleted, live])))

    assert deleted in merged
    assert any(m.metadata.get("message_count") == 1 for m in merged)
