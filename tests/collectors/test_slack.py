from datetime import datetime, timezone

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


def test_incremental_sync_preserves_prior_cursor_position_for_a_channel_with_no_new_messages():
    client = _FakeSlackClient()
    client.history_pages["general"] = [[{"ts": "300.0", "text": "newer message", "user": "U1"}]]
    client.history_pages["quiet"] = [[]]  # no new messages this round

    collector = SlackCollector(client=client, channels=["general", "quiet"])
    cursor = Cursor(data={"channels": {"general": "200.0", "quiet": "150.0"}})
    list(collector.incremental_sync(cursor))

    # "quiet" never called _track_latest this round - without seeding from
    # the incoming cursor, its prior position would be dropped here, and a
    # caller that persists this cursor (ingest.py's run_saas_ingest) would
    # then re-pull "quiet"'s entire history on the next sync.
    updated = collector.get_cursor().data["channels"]
    assert updated["quiet"] == "150.0"
    assert updated["general"] == "300.0"


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


def test_backfill_sync_passes_oldest_and_latest_and_filters_replies_outside_window():
    client = _FakeSlackClient()
    client.history_pages["general"] = [
        [
            {"ts": "100.0", "text": "in range", "user": "U1"},
            {"ts": "200.0", "text": "thread parent", "user": "U2", "thread_ts": "200.0", "reply_count": 2},
        ],
    ]
    client.replies[("general", "200.0")] = [
        {"ts": "200.0", "text": "thread parent", "user": "U2", "thread_ts": "200.0"},
        {"ts": "201.0", "text": "in-range reply", "user": "U3", "thread_ts": "200.0"},
        {"ts": "999.0", "text": "out-of-range reply", "user": "U1", "thread_ts": "200.0"},
    ]

    collector = SlackCollector(client=client, channels=["general"])
    since = datetime.fromtimestamp(100.0, tz=timezone.utc)
    until = datetime.fromtimestamp(250.0, tz=timezone.utc)
    items = list(collector.backfill_sync(since, until))

    texts = [item.content.decode("utf-8") for item in items]
    assert texts == ["in range", "thread parent", "in-range reply"]

    call = next(c for c in client.history_calls if c["channel"] == "general")
    assert call["oldest"] == "100.0"
    assert call["latest"] == "250.0"

    # backfill_sync must not disturb the incremental-sync cursor
    assert collector.get_cursor().data["channels"] == {}


def test_backfill_sync_without_until_omits_latest_param():
    client = _FakeSlackClient()
    client.history_pages["general"] = [[{"ts": "100.0", "text": "hi", "user": "U1"}]]

    collector = SlackCollector(client=client, channels=["general"])
    items = list(collector.backfill_sync(datetime.fromtimestamp(50.0, tz=timezone.utc)))

    assert len(items) == 1
    assert "latest" not in client.history_calls[0]


# --- SlackDirectory / 이름 해석 -------------------------------------------------


class _FakeDirectoryClient:
    """`conversations.info` / `users.list` 호출 횟수를 세는 가짜 클라이언트.
    SlackDirectory의 계약이 "채널당 1회, 유저는 워크스페이스 통틀어 1회"라 호출 수 자체가 검증 대상이다."""

    def __init__(self, channels=None, user_pages=None, fail=()):
        self._channels = channels or {}
        self._user_pages = user_pages or [[]]
        self._fail = set(fail)
        self.info_calls: list[str] = []
        self.users_calls: list[dict] = []

    def conversations_info(self, **kwargs):
        channel = kwargs["channel"]
        self.info_calls.append(channel)
        if "conversations_info" in self._fail or channel not in self._channels:
            raise RuntimeError("channel_not_found")
        return {"channel": {"id": channel, "name": self._channels[channel]}}

    def users_list(self, **kwargs):
        self.users_calls.append(kwargs)
        if "users_list" in self._fail:
            raise RuntimeError("missing_scope")
        index = int(kwargs.get("cursor") or 0)
        members = self._user_pages[index]
        next_cursor = str(index + 1) if index + 1 < len(self._user_pages) else None
        return {"members": members, "response_metadata": {"next_cursor": next_cursor} if next_cursor else {}}


def _directory(**kwargs):
    from knowledge_forge.collectors.saas.slack import SlackDirectory

    client = _FakeDirectoryClient(**kwargs)
    return SlackDirectory(client=client), client


def test_directory_caches_channel_lookups_one_call_per_channel():
    directory, client = _directory(channels={"C1": "project-intel-개발자"})

    assert directory.channel_name("C1") == "project-intel-개발자"
    assert directory.channel_name("C1") == "project-intel-개발자"
    assert client.info_calls == ["C1"]


def test_directory_loads_every_user_with_a_single_paginated_users_list():
    directory, client = _directory(
        user_pages=[
            [{"id": "U1", "profile": {"display_name": "이효현 (샨)"}, "real_name": "이효현"}],
            [{"id": "U2", "profile": {"display_name": ""}, "real_name": "Google Drive"}],
        ]
    )

    assert directory.user_name("U1") == "이효현 (샨)"
    # 봇 계정은 display_name이 빈 문자열이라 real_name으로 떨어진다.
    assert directory.user_name("U2") == "Google Drive"
    # 두 번째 유저를 물어도 users.list를 다시 부르지 않는다 (페이지 2개 = 호출 2번이 전부).
    assert len(client.users_calls) == 2


def test_directory_falls_back_to_project_id_when_conversations_info_has_no_scope():
    from knowledge_forge.collectors.saas.slack import SlackDirectory

    client = _FakeDirectoryClient(fail={"conversations_info"})
    directory = SlackDirectory(client=client, fallback_channel_names={"C1": "PRJ-0A09"})

    assert directory.channel_name("C1") == "PRJ-0A09"
    assert directory.channel_name("C2") is None  # 못 풀면 None - 푼 척하지 않는다
    # 실패해도 캐시되어 재호출하지 않는다.
    directory.channel_name("C1")
    assert client.info_calls == ["C1", "C2"]


def test_directory_user_names_keeps_raw_id_when_unresolvable():
    directory, _ = _directory(user_pages=[[{"id": "U1", "profile": {"display_name": "샨"}}]])

    assert directory.user_names(["U1", "U9", None]) == ["샨", "U9"]


def test_directory_resolve_mentions_rewrites_user_and_channel_links():
    directory, _ = _directory(
        channels={"C1": "general"},
        user_pages=[[{"id": "U1", "profile": {"display_name": "샨"}}]],
    )

    assert directory.resolve_mentions("<@U1> 님 <#C1|general> 확인") == "샨 님 #general 확인"
    # 못 푸는 멘션은 원문 그대로 남는다.
    assert directory.resolve_mentions("<@U9> 확인") == "<@U9> 확인"


def test_collector_adds_names_without_touching_raw_ids():
    from knowledge_forge.collectors.saas.slack import SlackDirectory

    client = _FakeSlackClient()
    client.history_pages["C1"] = [[{"ts": "100.0", "text": "<@U1> 확인 부탁", "user": "U1"}]]
    directory_client = _FakeDirectoryClient(
        channels={"C1": "general"}, user_pages=[[{"id": "U1", "profile": {"display_name": "샨"}}]]
    )
    collector = SlackCollector(
        client=client, channels=["C1"], directory=SlackDirectory(client=directory_client)
    )

    (item,) = list(collector.initial_sync())

    # 원시 ID는 그대로 - `channel`/`user`는 스레드 그룹핑과 조인의 키다.
    assert item.metadata["channel"] == "C1"
    assert item.metadata["user"] == "U1"
    assert item.metadata["channel_name"] == "general"
    assert item.metadata["user_name"] == "샨"
    assert item.metadata["text_resolved"] == "샨 확인 부탁"
    # content(=content_hash의 재료)는 건드리지 않는다 - 바뀌면 document_id가 통째로 달라진다.
    assert item.content == "<@U1> 확인 부탁".encode()


def test_collector_without_directory_produces_the_previous_metadata_exactly():
    client = _FakeSlackClient()
    client.history_pages["C1"] = [[{"ts": "100.0", "text": "hi", "user": "U1"}]]

    (item,) = list(SlackCollector(client=client, channels=["C1"]).initial_sync())

    assert item.metadata == {"channel": "C1", "ts": "100.0", "user": "U1", "thread_ts": None}
