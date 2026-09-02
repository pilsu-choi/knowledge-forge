from datetime import datetime, timezone
from typing import Any, Iterator

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType


def _ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


class SlackCollector(SaaSCollector):
    """Slack sync via `slack_sdk.WebClient` (injected, already authenticated).

    For each channel in `channels`: initial sync walks
    `conversations_history(channel=...)` paginated on `cursor`/
    `response_metadata.next_cursor`, and for every thread parent
    (`reply_count > 0`) also walks `conversations_replies(channel=...,
    ts=thread_ts)` for the replies. Incremental sync re-calls
    `conversations_history` with `oldest=<per-channel cursor timestamp>`.
    Slack has no native delete-event backfill via this API surface -
    deletions would need the Events API (`message_deleted`) wired
    separately; out of scope here, so `RawItem.deleted` is always False.
    """

    source_type = SourceType.SLACK

    def __init__(
        self,
        client: Any,
        channels: list[str],
        auth_config: SaaSAuthConfig | None = None,
        channel_project_map: dict[str, str] | None = None,
    ):
        """`channel_project_map` (channel id -> project_id) is optional - when
        given, it lets one SlackCollector instance poll channels that belong
        to different projects and still tag each RawItem with the right
        project_id (see RawItem.project_id / pipeline.run_pipeline). A
        channel missing from the map falls back to the run-level project_id,
        same as when no map is given at all.
        """
        super().__init__(client=client, auth_config=auth_config)
        self._channels = channels
        self._channel_project_map = channel_project_map or {}
        self._latest_ts: dict[str, str] = {}

    def _track_latest(self, channel: str, ts: str) -> None:
        current = self._latest_ts.get(channel)
        if current is None or float(ts) > float(current):
            self._latest_ts[channel] = ts

    def _make_item(self, channel: str, message: dict) -> RawItem:
        ts = message["ts"]
        text = message.get("text", "")
        data = text.encode("utf-8")
        return RawItem(
            uri=f"slack://{channel}/{ts}",
            content=data,
            content_hash=compute_content_hash(data),
            modified_at=_ts_to_datetime(ts),
            metadata={
                "channel": channel,
                "ts": ts,
                "user": message.get("user"),
                "thread_ts": message.get("thread_ts"),
            },
            deleted=False,
            project_id=self._channel_project_map.get(channel),
        )

    def _sync_channel(self, channel: str, oldest: str | None, latest: str | None = None) -> Iterator[RawItem]:
        def request_fn(cursor: str | None) -> tuple[list[dict], str | None]:
            kwargs: dict[str, Any] = {"channel": channel, "cursor": cursor}
            if oldest is not None:
                kwargs["oldest"] = oldest
            if latest is not None:
                kwargs["latest"] = latest
            response = self._client.conversations_history(**kwargs)
            messages = response["messages"]
            next_cursor = response.get("response_metadata", {}).get("next_cursor") or None
            return messages, next_cursor

        for message in self._paginate(request_fn):
            ts = message["ts"]
            self._track_latest(channel, ts)
            yield self._make_item(channel, message)

            is_thread_parent = message.get("thread_ts") == ts
            if is_thread_parent and message.get("reply_count", 0) > 0:
                replies_response = self._client.conversations_replies(channel=channel, ts=ts)
                for reply in replies_response["messages"]:
                    if reply["ts"] == ts:
                        continue  # conversations_replies includes the parent - already yielded
                    self._track_latest(channel, reply["ts"])
                    yield self._make_item(channel, reply)

    def initial_sync(self) -> Iterator[RawItem]:
        for channel in self._channels:
            yield from self._sync_channel(channel, oldest=None)

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        channel_cursors: dict[str, str] = cursor.data.get("channels", {})
        for channel in self._channels:
            oldest = channel_cursors.get(channel, "0")
            yield from self._sync_channel(channel, oldest=oldest)

    def get_cursor(self) -> Cursor:
        return Cursor(data={"channels": dict(self._latest_ts)})

    def backfill_sync(self, since: datetime, until: datetime | None = None) -> Iterator[RawItem]:
        """Pushes `since`/`until` down to Slack's `oldest`/`latest`
        `conversations_history` params instead of the base class's
        list-everything-then-filter default - avoids walking a channel's
        entire history just to backfill one window. Thread replies
        (`conversations_replies`) aren't bounded by `latest`, so every
        yielded item (parent and replies alike) is still filtered against
        `[since, until]` before being yielded, for correctness. `_sync_channel`
        updates `self._latest_ts` as a side effect of pagination (it's shared
        with initial_sync/incremental_sync) - snapshotted and restored around
        the loop below so backfill_sync leaves the incremental-sync cursor
        exactly as it found it.
        """
        latest_ts_snapshot = dict(self._latest_ts)
        oldest = str(since.timestamp())
        latest = str(until.timestamp()) if until is not None else None
        try:
            for channel in self._channels:
                for item in self._sync_channel(channel, oldest=oldest, latest=latest):
                    ts = _ts_to_datetime(item.metadata["ts"])
                    if ts >= since and (until is None or ts <= until):
                        yield item
        finally:
            self._latest_ts = latest_ts_snapshot
