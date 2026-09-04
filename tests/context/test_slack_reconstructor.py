from datetime import datetime, timezone

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.context.slack import SlackConversationReconstructor


def _msg(channel, ts, user, text, thread_ts=None, project_id=None):
    return RawItem(
        uri=f"slack://{channel}/{ts}",
        content=text.encode("utf-8"),
        content_hash="irrelevant",
        modified_at=datetime.fromtimestamp(float(ts), tz=timezone.utc),
        metadata={"channel": channel, "ts": ts, "user": user, "thread_ts": thread_ts},
        project_id=project_id,
    )


def _msg_named(channel, ts, user, text, **names):
    """`_msg` + SlackDirectory가 붙었을 때 수집기가 얹는 이름 키들."""
    item = _msg(channel, ts, user, text)
    item.metadata.update({k: v for k, v in names.items() if v is not None})
    return item


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


def test_thread_episode_propagates_the_messages_project_id():
    items = [
        _msg("general", "100.0", "A", "parent", thread_ts="100.0", project_id="PRJ-A"),
        _msg("general", "101.0", "B", "reply", thread_ts="100.0", project_id="PRJ-A"),
    ]

    episodes = list(SlackConversationReconstructor().reconstruct(iter(items)))

    assert len(episodes) == 1
    assert episodes[0].project_id == "PRJ-A"


def test_timeline_episode_propagates_the_messages_project_id():
    # Both from the same channel, so both carry the same
    # channel_project_map value in practice (a realistic fixture).
    items = [
        _msg("general", "0.0", "A", "first", project_id="PRJ-B"),
        _msg("general", "1.0", "A", "second", project_id="PRJ-B"),
    ]

    episodes = list(SlackConversationReconstructor().reconstruct(iter(items)))

    assert len(episodes) == 1
    assert episodes[0].project_id == "PRJ-B"


def test_no_project_id_configured_stays_none():
    items = [_msg("general", "0.0", "A", "first")]

    episodes = list(SlackConversationReconstructor().reconstruct(iter(items)))

    assert episodes[0].project_id is None


def test_merge_carries_display_names_and_first_message_without_dropping_raw_ids():
    """SlackDirectory가 붙은 수집기가 채운 이름을 에피소드 메타데이터로 옮기는지.
    원시 `channel`/`participants`는 조인 키라 그대로 남아야 한다
    (`storage/postgres_store.find_slack_thread_*`, `document_links.py`)."""
    items = [
        _msg_named("C1", "100.0", "U1", "<@U1> 확인 부탁", channel_name="general", user_name="샨", text_resolved="샨 확인 부탁"),
        _msg_named("C1", "120.0", "U2", "넵", channel_name="general", user_name="딘"),
    ]

    (episode,) = list(SlackConversationReconstructor().reconstruct(iter(items)))

    assert episode.metadata["channel"] == "C1"
    assert episode.metadata["participants"] == ["U1", "U2"]
    assert episode.metadata["channel_name"] == "general"
    assert episode.metadata["participant_names"] == ["샨", "딘"]
    # 첫 메시지는 멘션이 풀린 형태(text_resolved)를 우선한다 - 제목의 재료라서.
    assert episode.metadata["first_message"] == "샨 확인 부탁"


def test_merge_falls_back_to_raw_content_and_omits_name_keys_without_a_directory():
    items = [_msg("C1", "100.0", "U1", "안녕하세요\n  반갑습니다")]

    (episode,) = list(SlackConversationReconstructor().reconstruct(iter(items)))

    assert episode.metadata["first_message"] == "안녕하세요 반갑습니다"
    assert "channel_name" not in episode.metadata
    assert "participant_names" not in episode.metadata
