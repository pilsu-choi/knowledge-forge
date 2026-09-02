import base64
from datetime import datetime, timedelta, timezone

import pytest

from knowledge_forge.collectors.base import Cursor
from knowledge_forge.collectors.saas.gmail import GmailCollector


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeMessagesResource:
    def __init__(self, list_pages, get_by_id):
        self._list_pages = list(list_pages)
        self._get_by_id = get_by_id
        self.list_calls = []
        self.get_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _FakeExecutable(self._list_pages.pop(0))

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _FakeExecutable(self._get_by_id[kwargs["id"]])


class _FakeHistoryResource:
    def __init__(self, pages):
        self._pages = list(pages)
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _FakeExecutable(self._pages.pop(0))


class _FakeUsersResource:
    def __init__(self, messages_resource, profile, history_resource=None):
        self._messages = messages_resource
        self._profile = profile
        self._history = history_resource

    def messages(self):
        return self._messages

    def history(self):
        return self._history

    def getProfile(self, **kwargs):
        return _FakeExecutable(self._profile)


class _FakeGmailClient:
    def __init__(self, users_resource):
        self._users = users_resource

    def users(self):
        return self._users


def _text_plain_message(msg_id: str, text: str, thread_id: str = "t1", internal_date_ms: int = 1_700_000_000_000) -> dict:
    data = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": text[:20],
        "internalDate": str(internal_date_ms),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": f"Subject for {msg_id}"},
                {"name": "From", "value": "a@example.com"},
            ],
            "body": {"data": data},
        },
    }


def _snippet_only_message(msg_id: str, snippet: str) -> dict:
    return {
        "id": msg_id,
        "threadId": "t2",
        "snippet": snippet,
        "internalDate": "1700000001000",
        "payload": {"mimeType": "text/html", "headers": [], "body": {}},
    }


def test_initial_sync_paginates_and_fetches_full_messages():
    list_pages = [
        {"messages": [{"id": "m1"}], "nextPageToken": "p2"},
        {"messages": [{"id": "m2"}], "nextPageToken": None},
    ]
    get_by_id = {
        "m1": _text_plain_message("m1", "Hello world"),
        "m2": _snippet_only_message("m2", "a short preview"),
    }
    messages = _FakeMessagesResource(list_pages, get_by_id)
    users = _FakeUsersResource(messages, profile={"historyId": "999"})
    client = _FakeGmailClient(users)

    collector = GmailCollector(client=client, query="in:inbox")
    items = list(collector.initial_sync())

    assert len(items) == 2
    assert items[0].uri == "gmail://m1"
    assert items[0].content == b"Hello world"
    assert items[0].metadata["subject"] == "Subject for m1"
    assert items[0].metadata["thread_id"] == "t1"
    assert items[0].modified_at == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)

    assert items[1].uri == "gmail://m2"
    assert items[1].content == b"a short preview"  # fallback to snippet, no text/plain part

    assert messages.list_calls[0]["q"] == "in:inbox"
    assert messages.list_calls[1]["pageToken"] == "p2"

    cursor = collector.get_cursor()
    assert cursor.data["history_id"] == "999"


def test_incremental_sync_yields_added_and_deleted_messages():
    history_pages = [
        {
            "history": [
                {"messagesAdded": [{"message": {"id": "m3"}}]},
                {"messagesDeleted": [{"message": {"id": "m1"}}]},
            ],
            "nextPageToken": None,
        }
    ]
    get_by_id = {"m3": _text_plain_message("m3", "New message")}
    messages = _FakeMessagesResource([], get_by_id)
    history = _FakeHistoryResource(history_pages)
    users = _FakeUsersResource(messages, profile={"historyId": "1001"}, history_resource=history)
    client = _FakeGmailClient(users)

    collector = GmailCollector(client=client)
    cursor = Cursor(data={"history_id": "555"})
    items = list(collector.incremental_sync(cursor))

    assert len(items) == 2
    added = [i for i in items if not i.deleted]
    deleted = [i for i in items if i.deleted]
    assert len(added) == 1 and added[0].uri == "gmail://m3"
    assert len(deleted) == 1 and deleted[0].uri == "gmail://m1"
    assert deleted[0].content == b""

    assert history.list_calls[0]["startHistoryId"] == "555"
    assert collector.get_cursor().data["history_id"] == "1001"


def test_get_cursor_before_any_sync_raises_nothing_but_has_no_history_id():
    client = _FakeGmailClient(_FakeUsersResource(_FakeMessagesResource([], {}), profile={}))
    collector = GmailCollector(client=client)
    assert collector.get_cursor().data["history_id"] is None


def test_backfill_sync_builds_after_before_query_and_filters_by_exact_time():
    list_pages = [{"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": None}]
    get_by_id = {
        "m1": _text_plain_message("m1", "in window", internal_date_ms=1_700_050_000_000),
        "m2": _text_plain_message("m2", "outside window", internal_date_ms=1_700_000_000_000),
    }
    messages = _FakeMessagesResource(list_pages, get_by_id)
    users = _FakeUsersResource(messages, profile={"historyId": "999"})
    client = _FakeGmailClient(users)

    collector = GmailCollector(client=client, query="in:inbox")
    since = datetime.fromtimestamp(1_700_040_000, tz=timezone.utc)
    until = datetime.fromtimestamp(1_700_060_000, tz=timezone.utc)
    items = list(collector.backfill_sync(since, until))

    assert [i.uri for i in items] == ["gmail://m1"]

    query = messages.list_calls[0]["q"]
    assert "in:inbox" in query
    assert f"after:{since.strftime('%Y/%m/%d')}" in query
    assert f"before:{(until + timedelta(days=1)).strftime('%Y/%m/%d')}" in query

    # backfill_sync must never advance the incremental-sync history cursor
    assert collector.get_cursor().data["history_id"] is None


def test_backfill_sync_without_until_has_no_before_clause():
    list_pages = [{"messages": [], "nextPageToken": None}]
    messages = _FakeMessagesResource(list_pages, {})
    users = _FakeUsersResource(messages, profile={"historyId": "1"})
    client = _FakeGmailClient(users)

    collector = GmailCollector(client=client)
    list(collector.backfill_sync(datetime(2026, 8, 1, tzinfo=timezone.utc)))

    query = messages.list_calls[0]["q"]
    assert "before:" not in query
    assert "after:2026/08/01" in query
