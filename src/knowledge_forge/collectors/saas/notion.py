from typing import Any, Iterator

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.models.source import SourceType


class NotionCollector(SaaSCollector):
    """Interface-only: real Notion sync is not implemented yet.

    Intended implementation (notion_client.Client): initial sync calls
    `client.search(filter={"property": "object", "value": "page"},
    start_cursor=...)` (or `client.databases.query(database_id=...)` when
    `database_id` is given) to discover pages, then recursively calls
    `client.blocks.children.list(block_id=<page or block id>,
    start_cursor=...)` (drive pagination through `SaaSCollector._paginate`)
    to walk each page's block tree into content. Incremental sync filters
    on each page's `last_edited_time` against the cursor's last-synced
    timestamp - Notion's API has no dedicated delete feed, so a full
    re-list diffed against the prior manifest (similar to
    LocalFileCollector) is the practical approach for delete detection.
    """

    source_type = SourceType.NOTION

    def __init__(
        self,
        client: Any,
        database_id: str | None = None,
        auth_config: SaaSAuthConfig | None = None,
    ):
        super().__init__(client=client, auth_config=auth_config)
        self._database_id = database_id

    def initial_sync(self) -> Iterator[RawItem]:
        raise NotImplementedError(
            "NotionCollector.initial_sync: implement via client.search(...)/"
            "client.databases.query(...) + client.blocks.children.list(...). "
            "See class docstring."
        )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        raise NotImplementedError(
            "NotionCollector.incremental_sync: implement via last_edited_time filtering, "
            "diffed against the prior page-id manifest for delete detection. "
            "See class docstring."
        )

    def get_cursor(self) -> Cursor:
        raise NotImplementedError(
            "NotionCollector.get_cursor: persist the last-synced timestamp and the "
            "known page-id manifest as the cursor."
        )
