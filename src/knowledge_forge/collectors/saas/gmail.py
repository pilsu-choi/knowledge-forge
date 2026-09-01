from typing import Any, Iterator

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.models.source import SourceType


class GmailCollector(SaaSCollector):
    """Interface-only: real Gmail sync is not implemented yet.

    Intended implementation (google-api-python-client Gmail service):
    initial sync calls `client.users().messages().list(userId='me',
    q=query, pageToken=...)` (drive pagination through
    `SaaSCollector._paginate`) then `client.users().messages().get(
    userId='me', id=<message id>, format='full')` per message to fetch
    the full payload. Incremental sync narrows `query` with Gmail's
    `after:<date>` search operator seeded from the cursor's last-synced
    timestamp, or alternatively uses the Gmail History API
    (`users().history().list(startHistoryId=...)`) for a proper
    change/delete feed - History API is the more correct approach and
    should be preferred once implemented, since `q=after:` alone cannot
    detect deletions.
    """

    source_type = SourceType.GMAIL

    def __init__(
        self,
        client: Any,
        query: str | None = None,
        auth_config: SaaSAuthConfig | None = None,
    ):
        super().__init__(client=client, auth_config=auth_config)
        self._query = query

    def initial_sync(self) -> Iterator[RawItem]:
        raise NotImplementedError(
            "GmailCollector.initial_sync: implement via users().messages().list(...) "
            "+ users().messages().get(...). See class docstring."
        )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        raise NotImplementedError(
            "GmailCollector.incremental_sync: implement via users().history().list("
            "startHistoryId=...) or a narrowed q='after:<ts>' query. See class docstring."
        )

    def get_cursor(self) -> Cursor:
        raise NotImplementedError(
            "GmailCollector.get_cursor: persist the latest Gmail historyId (preferred) "
            "or last-synced timestamp as the cursor."
        )
