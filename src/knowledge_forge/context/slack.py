"""Slack Context Reconstruction: Conversation Reconstruction + Segmentation.

A single Slack message is rarely a complete semantic unit - see
wiki/roadmap.md's deployment-scheduling example, where the meaning of the
final message depends on several messages before it. This reconstructor
turns a channel's message stream into "episodes":

- Thread replies (`thread_ts` set, different from the message's own `ts`)
  are grouped with their parent into one episode - Slack already marks
  these as one conversation, so no time-based segmentation is needed.
- Remaining channel-timeline messages are ordered by `ts` and split into new
  episodes wherever the gap to the previous message exceeds `gap_seconds`
  ("Conversation Segmentation").

Each episode becomes one merged RawItem. Consecutive timeline episodes in
the same channel are additionally annotated with a `links` metadata entry
(`{"type": ..., "target_uri": <previous episode's uri>}`) - the generic
convention `pipeline.run_pipeline` resolves into `Link` records once each
episode's Source/Document exist (see wiki/okf.md's "그래프 뷰어 대비"
decision: episode-to-episode edges can't be expressed by `Relation`, which
only ever connects `KnowledgeObject`s produced within one document). Thread
episodes are deliberately left unlinked here - reconciling their yield
order against the timeline's chronology is a separate, harder problem left
for later.
"""

from collections import defaultdict
from collections.abc import Iterator

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.context.base import ContextReconstructor
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.link import LinkType
from knowledge_forge.storage.document_title import first_substantive_message

_DEFAULT_GAP_SECONDS = 1800.0

# 에피소드 첫 메시지에서 `sources.metadata.first_message`로 실어 둘 원문 길이 상한.
# 실제 제목에 쓰이는 발췌 길이(`storage/document_title.py::_SLACK_EXCERPT_CHARS`)보다
# 넉넉하게 잡는다 - 제목 규칙이 바뀌어 발췌를 늘리고 싶어질 때 재적재 없이
# `db backfill-titles --overwrite`만으로 넓힐 수 있게 하려는 여유분이다.
_FIRST_MESSAGE_CHARS = 400


def _ts(item: RawItem) -> float:
    return float(item.metadata["ts"])


def _format_message(item: RawItem) -> str:
    user = item.metadata.get("user", "unknown")
    text = item.content.decode("utf-8", errors="replace")
    return f"{user}: {text}"


class SlackConversationReconstructor(ContextReconstructor):
    def __init__(self, gap_seconds: float = _DEFAULT_GAP_SECONDS):
        self._gap_seconds = gap_seconds

    def reconstruct(self, items: Iterator[RawItem]) -> Iterator[RawItem]:
        channels: dict[str, list[RawItem]] = defaultdict(list)
        for item in items:
            if item.deleted:
                yield item
                continue
            channels[item.metadata["channel"]].append(item)

        for channel, messages in channels.items():
            yield from self._reconstruct_channel(channel, messages)

    def _reconstruct_channel(self, channel: str, messages: list[RawItem]) -> Iterator[RawItem]:
        by_ts = {m.metadata["ts"]: m for m in messages}
        replies_by_thread: dict[str, list[RawItem]] = defaultdict(list)
        consumed_ts: set[str] = set()

        for m in messages:
            thread_ts = m.metadata.get("thread_ts")
            if thread_ts is not None and thread_ts != m.metadata["ts"]:
                replies_by_thread[thread_ts].append(m)

        for thread_ts, replies in replies_by_thread.items():
            episode = []
            parent = by_ts.get(thread_ts)
            if parent is not None:
                episode.append(parent)
                consumed_ts.add(thread_ts)
            episode.extend(sorted(replies, key=_ts))
            consumed_ts.update(r.metadata["ts"] for r in replies)
            yield self._merge(channel, episode, thread_ts=thread_ts)

        remaining = sorted(
            (m for m in messages if m.metadata["ts"] not in consumed_ts), key=_ts
        )
        previous_uri: str | None = None
        for episode in self._segment(remaining):
            item = self._merge(channel, episode, thread_ts=None, previous_uri=previous_uri)
            previous_uri = item.uri
            yield item

    def _segment(self, ordered_messages: list[RawItem]) -> Iterator[list[RawItem]]:
        episode: list[RawItem] = []
        previous_ts: float | None = None
        for message in ordered_messages:
            ts = _ts(message)
            if episode and previous_ts is not None and ts - previous_ts > self._gap_seconds:
                yield episode
                episode = []
            episode.append(message)
            previous_ts = ts
        if episode:
            yield episode

    def _merge(
        self,
        channel: str,
        episode: list[RawItem],
        thread_ts: str | None,
        previous_uri: str | None = None,
    ) -> RawItem:
        content = "\n\n".join(_format_message(m) for m in episode).encode("utf-8")
        participants = list(
            dict.fromkeys(m.metadata.get("user") for m in episode if m.metadata.get("user"))
        )
        message_ts = [m.metadata["ts"] for m in episode]
        anchor = thread_ts if thread_ts is not None else message_ts[0]
        kind = "thread" if thread_ts is not None else "episode"
        metadata = {
            "channel": channel,
            "message_ts": message_ts,
            "participants": participants,
            "message_count": len(episode),
        }
        # SlackDirectory가 붙어 있을 때만 채워지는 사람이 읽는 이름들
        # (collectors/saas/slack.py::SlackDirectory, wiki/saas-connectors.md).
        # 원시 ID는 `channel`/`participants`에 그대로 남는다 - 둘 다 조인 키다.
        channel_name = next(
            (m.metadata.get("channel_name") for m in episode if m.metadata.get("channel_name")), None
        )
        if channel_name:
            metadata["channel_name"] = channel_name
        participant_names = list(
            dict.fromkeys(m.metadata.get("user_name") for m in episode if m.metadata.get("user_name"))
        )
        if participant_names:
            metadata["participant_names"] = participant_names
        # 에피소드 첫 메시지 원문 - `documents.title`의 재료다. 이게 없으면 슬랙 제목은
        # "#채널 날짜"에 머물고, v0 골든셋 실측상 그 형태는 슬랙 정답 66건 중 0건에
        # 닿는다.
        # `episode[0]`이 아니라 "내용 있는 첫 메시지"다 - 채널 개설 직후 에피소드는
        # "…님이 채널에 참여함" 가입 알림으로 시작하는 일이 흔하고, 그걸 제목에 실으면
        # 검색 이득 0에 pg_trgm 표면적만 는다(`document_title.first_substantive_message`).
        first_text = first_substantive_message(
            m.metadata.get("text_resolved") or m.content.decode("utf-8", errors="replace") for m in episode
        )
        first_text = " ".join((first_text or "").split())
        if first_text:
            metadata["first_message"] = first_text[:_FIRST_MESSAGE_CHARS]
        if thread_ts is not None:
            metadata["thread_ts"] = thread_ts
        if previous_uri is not None:
            metadata["links"] = [{"type": LinkType.SAME_CHANNEL_SEQUENCE.value, "target_uri": previous_uri}]
        return RawItem(
            uri=f"slack://{channel}/{kind}/{anchor}",
            content=content,
            content_hash=compute_content_hash(content),
            modified_at=max(m.modified_at for m in episode),
            metadata=metadata,
            deleted=False,
            # All of an episode's messages come from the same `channel`, so
            # they carry the same per-channel project_id (SlackCollector's
            # channel_project_map) - propagate it, or the merged item
            # defaults to None and pipeline.py silently falls back to the
            # run-level project_id for every Slack source, regardless of
            # KF_SLACK_CHANNEL_PROJECT_MAP.
            project_id=episode[0].project_id,
        )
