from datetime import datetime, timezone
from typing import Any, Iterator

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class NotionCollector(SaaSCollector):
    """Notion sync via `notion_client.Client` (injected, already authenticated).

    Page discovery: `client.search(filter={"property": "object", "value":
    "page"}, ...)`, or `client.databases.query(database_id=...)` when
    `database_id` is given - both paginated on `start_cursor`/`next_cursor`/
    `has_more`. Content: each page's block tree is walked recursively via
    `client.blocks.children.list(block_id=...)` (same pagination shape) and
    flattened to plain text - a pragmatic "extract what obviously has
    rich_text" pass, not a full block-type-by-block-type transcription.

    Notion's API has no dedicated delete feed, so incremental sync detects
    deletions by diffing the currently-discovered page-id set against the
    prior sync's known-page-id set (the same manifest-diff principle
    LocalFileCollector uses for the filesystem).
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
        self._known_page_ids: dict[str, str] = {}
        self._last_synced_at: str | None = None

    def _discover_pages(self) -> Iterator[dict]:
        def request_fn(cursor: str | None) -> tuple[list[dict], str | None]:
            kwargs: dict[str, Any] = {"start_cursor": cursor} if cursor else {}
            if self._database_id:
                response = self._client.databases.query(database_id=self._database_id, **kwargs)
            else:
                response = self._client.search(
                    filter={"property": "object", "value": "page"}, **kwargs
                )
            next_cursor = response.get("next_cursor") if response.get("has_more") else None
            return response["results"], next_cursor

        yield from self._paginate(request_fn)

    @staticmethod
    def _extract_rich_text(block: dict) -> str:
        block_type = block.get("type")
        if not block_type:
            return ""
        rich_text = block.get(block_type, {}).get("rich_text")
        if not rich_text:
            return ""
        return "".join(rt.get("plain_text", "") for rt in rich_text)

    def _extract_page_text(self, page_id: str) -> str:
        lines: list[str] = []

        def collect(block_id: str) -> None:
            def request_fn(cursor: str | None) -> tuple[list[dict], str | None]:
                kwargs: dict[str, Any] = {"start_cursor": cursor} if cursor else {}
                response = self._client.blocks.children.list(block_id=block_id, **kwargs)
                next_cursor = response.get("next_cursor") if response.get("has_more") else None
                return response["results"], next_cursor

            for block in self._paginate(request_fn):
                text = self._extract_rich_text(block)
                if text:
                    lines.append(text)
                if block.get("has_children"):
                    collect(block["id"])

        collect(page_id)
        return "\n".join(lines)

    @staticmethod
    def _extract_title(page: dict) -> str | None:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                text = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                return text or None
        return None

    def _make_item(self, page: dict) -> RawItem:
        page_id = page["id"]
        text = self._extract_page_text(page_id)
        data = text.encode("utf-8")
        last_edited = page.get("last_edited_time")
        return RawItem(
            uri=page.get("url") or f"notion://{page_id}",
            content=data,
            content_hash=compute_content_hash(data),
            modified_at=_parse_iso(last_edited) if last_edited else datetime.now(timezone.utc),
            metadata={
                "page_id": page_id,
                "last_edited_time": last_edited,
                "title": self._extract_title(page),
            },
            deleted=False,
        )

    def initial_sync(self) -> Iterator[RawItem]:
        known: dict[str, str] = {}
        last_synced_at: str | None = None
        for page in self._discover_pages():
            page_id = page["id"]
            last_edited = page.get("last_edited_time", "")
            known[page_id] = last_edited
            if last_synced_at is None or last_edited > last_synced_at:
                last_synced_at = last_edited
            yield self._make_item(page)
        self._known_page_ids = known
        self._last_synced_at = last_synced_at

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        prior_known: dict[str, str] = cursor.data.get("known_page_ids", {})
        last_synced_at: str | None = cursor.data.get("last_synced_at")
        current_known: dict[str, str] = {}
        newest_seen = last_synced_at

        for page in self._discover_pages():
            page_id = page["id"]
            last_edited = page.get("last_edited_time", "")
            current_known[page_id] = last_edited
            if newest_seen is None or last_edited > newest_seen:
                newest_seen = last_edited
            if last_synced_at is None or last_edited > last_synced_at:
                yield self._make_item(page)

        for page_id in prior_known:
            if page_id not in current_known:
                yield RawItem(
                    uri=f"notion://{page_id}",
                    content=b"",
                    content_hash="",
                    modified_at=datetime.now(timezone.utc),
                    metadata={"page_id": page_id},
                    deleted=True,
                )

        self._known_page_ids = current_known
        self._last_synced_at = newest_seen

    def get_cursor(self) -> Cursor:
        return Cursor(
            data={
                "last_synced_at": self._last_synced_at,
                "known_page_ids": dict(self._known_page_ids),
            }
        )
