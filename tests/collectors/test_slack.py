from knowledge_forge.collectors.base import Cursor
from knowledge_forge.collectors.saas.slack import SlackCollector


class _FakeSlackClient:
    def __init__(self):
        self.history_pages: dict[str, list[dict]] = {}
        self.replies: dict[tuple[str, str], list[dict]] = {}
        self.history_calls: list[dict] = []

    def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        channel = kwargs["channel"]
        cursor = kwargs.get("cursor")
        pages = self.history_pages[channel]
        page_index = 0 if cursor is None else int(cursor)
        page = pages[page_index]
        next_cursor = str(page_index + 1) if page_index + 1 < len(pages) else None
        return {
            "messages": page,
            "response_metadata": {"next_cursor": next_cursor} if next_cursor else {},
        }

    def conversations_replies(self, **kwargs):
        return {"messages": self.replies[(kwargs["channel"], kwargs["ts"])]}


def test_initial_sync_paginates_and_includes_thread_replies_without_duplicating_parent():
    client = _FakeSlackClient()
    client.history_pages["general"] = [
        [{"ts": "100.0", "text": "hello", "user": "U1"}],
        [{"ts": "200.0", "text": "thread parent", "user": "U2", "thread_ts": "200.0", "reply_count": 2}],
    ]
    client.history_pages["random"] = [
        [{"ts": "50.0", "text": "random msg", "user": "U3"}],
    ]
    client.replies[("general", "200.0")] = [
        {"ts": "200.0", "text": "thread parent", "user": "U2", "thread_ts": "200.0"},
        {"ts": "201.0", "text": "reply one", "user": "U3", "thread_ts": "200.0"},
        {"ts": "202.0", "text": "reply two", "user": "U1", "thread_ts": "200.0"},
    ]

    collector = SlackCollector(client=client, channels=["general", "random"])
    items = list(collector.initial_sync())

    texts = [item.content.decode("utf-8") for item in items]
    assert texts == ["hello", "thread parent", "reply one", "reply two", "random msg"]
    assert all(not item.deleted for item in items)
    assert items[0].uri == "slack://general/100.0"
    assert items[1].metadata["thread_ts"] == "200.0"

    # pagination actually happened (two calls for #general)
    general_calls = [c for c in client.history_calls if c["channel"] == "general"]
    assert len(general_calls) == 2

    cursor = collector.get_cursor()
    assert cursor.data["channels"]["general"] == "202.0"
    assert cursor.data["channels"]["random"] == "50.0"


def test_incremental_sync_passes_oldest_from_cursor():
    client = _FakeSlackClient()
    client.history_pages["general"] = [[{"ts": "300.0", "text": "newer message", "user": "U1"}]]
    client.history_pages["random"] = [[]]

    collector = SlackCollector(client=client, channels=["general", "random"])
    cursor = Cursor(data={"channels": {"general": "200.0"}})
    items = list(collector.incremental_sync(cursor))

    assert len(items) == 1
    assert items[0].content == b"newer message"

    general_call = next(c for c in client.history_calls if c["channel"] == "general")
    assert general_call["oldest"] == "200.0"
    random_call = next(c for c in client.history_calls if c["channel"] == "random")
    assert random_call["oldest"] == "0"  # no prior cursor for this channel


def test_slack_never_yields_deleted_items():
    client = _FakeSlackClient()
    client.history_pages["general"] = [[{"ts": "1.0", "text": "hi", "user": "U1"}]]

    collector = SlackCollector(client=client, channels=["general"])
    items = list(collector.initial_sync())

    assert all(not item.deleted for item in items)


def test_channel_project_map_tags_items_per_channel():
    client = _FakeSlackClient()
    client.history_pages["general"] = [[{"ts": "1.0", "text": "hi", "user": "U1"}]]
    client.history_pages["random"] = [[{"ts": "2.0", "text": "yo", "user": "U2"}]]

    collector = SlackCollector(
        client=client,
        channels=["general", "random"],
        channel_project_map={"general": "PRJ-A"},
    )
    items = {item.metadata["channel"]: item for item in collector.initial_sync()}

    assert items["general"].project_id == "PRJ-A"
    assert items["random"].project_id is None  # not in the map - falls back to run-level project_id


def test_no_channel_project_map_leaves_project_id_unset():
    client = _FakeSlackClient()
    client.history_pages["general"] = [[{"ts": "1.0", "text": "hi", "user": "U1"}]]

    collector = SlackCollector(client=client, channels=["general"])
    items = list(collector.initial_sync())

    assert items[0].project_id is None
