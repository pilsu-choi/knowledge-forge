"""Gmail Context Reconstruction: Thread -> semantic unit.

A single email in isolation often makes no sense (a one-line reply, a
"sounds good" with no visible antecedent). Gmail's `threadId` already groups
related messages, so this reconstructor buffers a source's item stream,
groups by `metadata["thread_id"]`, sorts each group chronologically, and
merges it into one RawItem representing the whole thread.
"""

from collections import defaultdict
from typing import Iterator

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.context.base import ContextReconstructor
from knowledge_forge.ids import content_hash as compute_content_hash


def _decode(item: RawItem) -> str:
    return item.content.decode("utf-8", errors="replace")


def _format_message(item: RawItem) -> str:
    from_ = item.metadata.get("from", "unknown")
    date = item.metadata.get("date", "")
    header = f"From: {from_} ({date})" if date else f"From: {from_}"
    return f"{header}\n{_decode(item)}"


class GmailThreadReconstructor(ContextReconstructor):
    def reconstruct(self, items: Iterator[RawItem]) -> Iterator[RawItem]:
        threads: dict[str, list[RawItem]] = defaultdict(list)
        for item in items:
            if item.deleted:
                yield item
                continue
            thread_id = item.metadata.get("thread_id") or item.uri
            threads[thread_id].append(item)

        for thread_id, messages in threads.items():
            yield self._merge(thread_id, messages)

    def _merge(self, thread_id: str, messages: list[RawItem]) -> RawItem:
        ordered = sorted(messages, key=lambda m: m.modified_at)
        content = "\n\n".join(_format_message(m) for m in ordered).encode("utf-8")
        participants = list(
            dict.fromkeys(m.metadata.get("from") for m in ordered if m.metadata.get("from"))
        )
        return RawItem(
            uri=f"gmail://thread/{thread_id}",
            content=content,
            content_hash=compute_content_hash(content),
            modified_at=max(m.modified_at for m in ordered),
            metadata={
                "thread_id": thread_id,
                "message_ids": [m.metadata.get("message_id") for m in ordered],
                "subject": ordered[0].metadata.get("subject"),
                "participants": participants,
                "message_count": len(ordered),
            },
            deleted=False,
        )
