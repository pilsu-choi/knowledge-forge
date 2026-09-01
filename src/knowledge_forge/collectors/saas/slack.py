from typing import Any, Iterator

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.models.source import SourceType


class SlackCollector(SaaSCollector):
    """Interface-only: real Slack sync is not implemented yet.

    Intended implementation: for each channel in `channels`, initial sync
    calls `slack_sdk.WebClient.conversations_history(channel=...)` paginated
    via its `cursor`/`response_metadata.next_cursor` field (drive through
    `SaaSCollector._paginate`), and for every message with `thread_ts` also
    calls `conversations_replies(channel=..., ts=thread_ts)` to pull the
    thread. Incremental sync re-calls `conversations_history` with
    `oldest=<cursor timestamp>` to only fetch messages newer than the last
    sync. Slack has no native delete-event backfill via this API surface -
    deletions would need the Events API (`message_deleted`) wired
    separately; out of scope for this interface.
    """

    source_type = SourceType.SLACK

    def __init__(
        self,
        client: Any,
        channels: list[str],
        auth_config: SaaSAuthConfig | None = None,
    ):
        super().__init__(client=client, auth_config=auth_config)
        self._channels = channels

    def initial_sync(self) -> Iterator[RawItem]:
        raise NotImplementedError(
            "SlackCollector.initial_sync: implement via slack_sdk.WebClient."
            "conversations_history(channel=...) + conversations_replies(...) for threads. "
            "See class docstring."
        )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        raise NotImplementedError(
            "SlackCollector.incremental_sync: implement via conversations_history"
            "(channel=..., oldest=<cursor timestamp>). See class docstring."
        )

    def get_cursor(self) -> Cursor:
        raise NotImplementedError(
            "SlackCollector.get_cursor: persist the latest processed message "
            "timestamp per channel as the cursor."
        )
