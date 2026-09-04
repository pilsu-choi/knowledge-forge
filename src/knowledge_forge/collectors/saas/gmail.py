import base64
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType


def _find_header(headers: list[dict[str, str]], name: str) -> str | None:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value")
    return None


def _decode_body_data(data: str) -> str:
    # Gmail's body payloads are base64url without guaranteed padding.
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_plain_text(payload: dict[str, Any]) -> str | None:
    body = payload.get("body") or {}
    if payload.get("mimeType") == "text/plain" and body.get("data"):
        return _decode_body_data(body["data"])
    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text is not None:
            return text
    return None


def _message_content(message: dict[str, Any]) -> bytes:
    payload = message.get("payload") or {}
    text = _extract_plain_text(payload)
    if text is None:
        # No text/plain part found (e.g. HTML-only mail) - fall back to
        # Gmail's own short plain-text preview rather than parsing HTML.
        text = message.get("snippet", "")
    return text.encode("utf-8")


def _message_modified_at(message: dict[str, Any]) -> datetime:
    internal_date_ms = int(message["internalDate"])
    return datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)


def _message_metadata(message: dict[str, Any]) -> dict[str, Any]:
    headers = (message.get("payload") or {}).get("headers", []) or []
    metadata: dict[str, Any] = {
        "message_id": message["id"],
        "thread_id": message.get("threadId"),
    }
    for header_name, key in (("Subject", "subject"), ("From", "from"), ("Date", "date")):
        value = _find_header(headers, header_name)
        if value is not None:
            metadata[key] = value
    return metadata


def _raw_item_for_message(message: dict[str, Any]) -> RawItem:
    content = _message_content(message)
    return RawItem(
        uri=f"gmail://{message['id']}",
        content=content,
        content_hash=compute_content_hash(content),
        modified_at=_message_modified_at(message),
        metadata=_message_metadata(message),
        deleted=False,
    )


class GmailCollector(SaaSCollector):
    """Real Gmail sync via google-api-python-client.

    Initial sync lists + fetches every message matching `query`. Incremental
    sync uses the Gmail History API (`users().history().list`), which - unlike
    a narrowed `q=after:` search - natively reports deletions, so it is
    preferred over query-narrowing as the class docstring originally noted.
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
        self._history_id: str | None = None

    def _refresh_history_id(self) -> None:
        profile = self._client.users().getProfile(userId="me").execute()
        self._history_id = profile.get("historyId")

    def _fetch_message(self, message_id: str) -> dict[str, Any]:
        return self._client.users().messages().get(userId="me", id=message_id, format="full").execute()

    def initial_sync(self) -> Iterator[RawItem]:
        def request_fn(cursor: str | None) -> tuple[list[dict], str | None]:
            response = self._client.users().messages().list(
                userId="me", q=self._query, pageToken=cursor
            ).execute()
            return response.get("messages", []), response.get("nextPageToken")

        for summary in self._paginate(request_fn):
            message = self._fetch_message(summary["id"])
            yield _raw_item_for_message(message)

        self._refresh_history_id()

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        start_history_id = cursor.data["history_id"]

        def request_fn(page_cursor: str | None) -> tuple[list[dict], str | None]:
            response = self._client.users().history().list(
                userId="me", startHistoryId=start_history_id, pageToken=page_cursor
            ).execute()
            return response.get("history", []), response.get("nextPageToken")

        for record in self._paginate(request_fn):
            for added in record.get("messagesAdded", []):
                message = self._fetch_message(added["message"]["id"])
                yield _raw_item_for_message(message)
            for deleted in record.get("messagesDeleted", []):
                message_id = deleted["message"]["id"]
                yield RawItem(
                    uri=f"gmail://{message_id}",
                    content=b"",
                    content_hash="",
                    modified_at=datetime.now(tz=timezone.utc),
                    metadata={"message_id": message_id},
                    deleted=True,
                )

        self._refresh_history_id()

    def get_cursor(self) -> Cursor:
        return Cursor(data={"history_id": self._history_id})

    def backfill_sync(self, since: datetime, until: datetime | None = None) -> Iterator[RawItem]:
        """Pushes `since`/`until` down into the `messages.list` search query
        instead of the base class's list-everything-then-filter default -
        avoids paginating the entire mailbox just to backfill one window.

        Gmail's `after:`/`before:` search operators only have day precision
        (no time-of-day), so the query widens to whole-day boundaries and
        every fetched message's exact `internalDate` is re-filtered against
        `since`/`until` before being yielded - giving the caller real
        hour/minute precision despite the query itself being day-granular.
        `until`'s day is widened by one, since Gmail's `before:` is exclusive
        of the given date.

        Does not call `_refresh_history_id()` - backfill must never disturb
        the incremental-sync history cursor.
        """
        query_parts = [self._query] if self._query else []
        query_parts.append(f"after:{since.strftime('%Y/%m/%d')}")
        if until is not None:
            before_date = until + timedelta(days=1)
            query_parts.append(f"before:{before_date.strftime('%Y/%m/%d')}")
        query = " ".join(query_parts)

        def request_fn(cursor: str | None) -> tuple[list[dict], str | None]:
            response = self._client.users().messages().list(
                userId="me", q=query, pageToken=cursor
            ).execute()
            return response.get("messages", []), response.get("nextPageToken")

        for summary in self._paginate(request_fn):
            message = self._fetch_message(summary["id"])
            item = _raw_item_for_message(message)
            if item.modified_at >= since and (until is None or item.modified_at <= until):
                yield item
