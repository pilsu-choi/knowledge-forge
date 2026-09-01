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

Each episode becomes one merged RawItem.
"""

from collections import defaultdict
from typing import Iterator

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.context.base import ContextReconstructor
from knowledge_forge.ids import content_hash as compute_content_hash

_DEFAULT_GAP_SECONDS = 1800.0


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
        for episode in self._segment(remaining):
            yield self._merge(channel, episode, thread_ts=None)

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

    def _merge(self, channel: str, episode: list[RawItem], thread_ts: str | None) -> RawItem:
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
        if thread_ts is not None:
            metadata["thread_ts"] = thread_ts
        return RawItem(
            uri=f"slack://{channel}/{kind}/{anchor}",
            content=content,
            content_hash=compute_content_hash(content),
            modified_at=max(m.modified_at for m in episode),
            metadata=metadata,
            deleted=False,
        )
