"""Gmail Context Reconstruction: Thread -> semantic unit.

A single email in isolation often makes no sense (a one-line reply, a
"sounds good" with no visible antecedent). Gmail's `threadId` already groups
related messages, so this reconstructor buffers a source's item stream,
groups by `metadata["thread_id"]`, sorts each group chronologically, and
merges it into one RawItem representing the whole thread.

Threads are then themselves ordered chronologically (by each thread's latest
message) and consecutive ones annotated with a `links` metadata entry
(`{"type": ..., "target_uri": <previous thread's uri>}`) - the same generic
convention `pipeline.run_pipeline` resolves into `Link` records that
context/slack.py uses for same-channel episode sequencing (see wiki/okf.md's
"그래프 뷰어 대비" decision). Gmail has no channel-like sub-grouping, so the
chain spans every thread this reconstruction call sees.
"""

from collections import defaultdict
from collections.abc import Iterator

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.context.base import ContextReconstructor
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.link import LinkType


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

        ordered_threads = sorted(
            threads.items(), key=lambda kv: max(m.modified_at for m in kv[1])
        )
        previous_uri: str | None = None
        for thread_id, messages in ordered_threads:
            item = self._merge(thread_id, messages, previous_uri=previous_uri)
            previous_uri = item.uri
            yield item

    def _merge(
        self, thread_id: str, messages: list[RawItem], previous_uri: str | None = None
    ) -> RawItem:
        ordered = sorted(messages, key=lambda m: m.modified_at)
        content = "\n\n".join(_format_message(m) for m in ordered).encode("utf-8")
        participants = list(
            dict.fromkeys(m.metadata.get("from") for m in ordered if m.metadata.get("from"))
        )
        metadata = {
            "thread_id": thread_id,
            "message_ids": [m.metadata.get("message_id") for m in ordered],
            "subject": ordered[0].metadata.get("subject"),
            "participants": participants,
            "message_count": len(ordered),
        }
        if previous_uri is not None:
            metadata["links"] = [{"type": LinkType.THREAD_SEQUENCE.value, "target_uri": previous_uri}]
        return RawItem(
            uri=f"gmail://thread/{thread_id}",
            content=content,
            content_hash=compute_content_hash(content),
            modified_at=max(m.modified_at for m in ordered),
            metadata=metadata,
            deleted=False,
            # Propagate the thread's messages' project_id (same fix as
            # context/slack.py's _merge) - GmailCollector doesn't set this
            # today, but a merged RawItem silently dropping it to None would
            # defeat any future per-item tenancy tagging the same way it did
            # for Slack's channel_project_map.
            project_id=ordered[0].project_id,
        )
