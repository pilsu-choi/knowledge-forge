import logging
import re
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType

logger = logging.getLogger(__name__)

# Slack 본문의 멘션/채널 링크 표기 - `<@U0BCCD1TNMD>`, `<#C0BM4AZLCEB|채널명>`.
_MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_LINK_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|[^>]*)?>")

# `users.list` 한 페이지 크기. Slack 권장 상한이 200이고, 이 워크스페이스는 유저 305명이라
# 두 페이지면 끝난다.
_USERS_PAGE_SIZE = 200


def _ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _display_name(member: dict) -> str | None:
    """한 `users.list` 멤버에서 사람이 부르는 이름 하나를 고른다.

    `profile.display_name` -> `real_name` -> `name` 순. 이 워크스페이스 실측으로
    display_name은 "이효현 (샨)"처럼 사내 호칭까지 담겨 있어 검색 질의가 가장 잘 맞는
    형태고, 봇 계정은 display_name이 빈 문자열이라 real_name("Google Drive")으로 떨어진다.
    셋 다 없으면 None을 돌려주고 호출부가 원시 ID를 그대로 쓴다 - 못 푼 걸 푼 척하지 않는다."""
    profile = member.get("profile") or {}
    for candidate in (profile.get("display_name"), member.get("real_name"), member.get("name")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


class SlackDirectory:
    """채널 ID / 유저 ID -> 사람이 읽는 이름. Web API 호출을 최소로 묶어 캐시한다.

    왜 있는가: Slack이 주는 원본 레코드에는 `C0BGQEQG1L6` / `U0BBCPVEHF0` 같은 불투명한
    ID밖에 없다. 그대로 저장하면 `documents.title`이 "#C0BGQEQG1L6 2026-09-01"이 되고,
    `sources.metadata.participants`가 원시 유저 ID 배열이 되어, 제목 조회
    (`search_documents_by_title`)와 작성자/참여자 필터
    (`search/retrieve.py::_metadata_filter_where`)가 슬랙 문서에 **구조적으로** 닿지
    못한다(개발 DB 197건 전부가 그 상태였다). 근거는 wiki/saas-connectors.md.

    호출 예산:
    - 채널은 `conversations.info`를 채널당 딱 1회(채널 12개 = 12콜) 부르고 캐시한다.
      `conversations.list`로 한 번에 받는 방법도 있지만 그건 워크스페이스 전체 채널을
      페이지네이션하는 것이라, 우리가 보는 채널이 12개뿐일 때는 오히려 비싸다.
    - 유저는 `users.list`를 **워크스페이스 통틀어 1회**(페이지네이션 포함) 불러 전부
      캐시한다. `users.info`를 참여자마다 부르면 스레드 수만큼 콜이 늘어난다.

    실패는 삼킨다. 토큰에 `channels:read`/`users:read` 스코프가 없거나 채널이 아카이브
    되었으면 경고 로그만 남기고 None을 캐시해 다시 부르지 않는다 - 이름 해석은 검색 품질
    개선이지 수집의 전제 조건이 아니라서, 스코프가 없다고 적재가 멈추면 안 된다.
    """

    def __init__(self, client: Any, fallback_channel_names: dict[str, str] | None = None):
        self._client = client
        self._fallback_channel_names = fallback_channel_names or {}
        self._channels: dict[str, str | None] = {}
        self._users: dict[str, str | None] | None = None

    def channel_name(self, channel_id: str) -> str | None:
        if channel_id in self._channels:
            return self._channels[channel_id]
        name: str | None = None
        try:
            response = self._client.conversations_info(channel=channel_id)
            name = ((response["channel"] or {}) or {}).get("name")
        except Exception as exc:  # noqa: BLE001 - 이름 해석 실패로 수집을 멈추지 않는다
            logger.warning("slack conversations.info failed for %s: %s", channel_id, exc)
        if not name:
            # 스코프가 없어 API로 못 풀면 `KF_SLACK_CHANNEL_PROJECT_MAP`이 주는 프로젝트 ID를
            # 마지막 수단으로 쓴다. 채널명만큼 좋진 않지만 원시 채널 ID보다는 질의에 나올
            # 확률이 있는 문자열이다.
            name = self._fallback_channel_names.get(channel_id)
        self._channels[channel_id] = name or None
        return self._channels[channel_id]

    def user_name(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        return self._load_users().get(user_id)

    def user_names(self, user_ids: Iterable[str | None]) -> list[str]:
        """ID 목록을 이름 목록으로. 못 푼 ID는 **원시 ID를 그대로 남긴다** - 목록에서
        조용히 사라지면 참여자 수가 달라져 `message_count`/`participants` 길이 관계가
        깨지고, 무엇이 안 풀렸는지도 알 수 없게 된다."""
        names: list[str] = []
        for user_id in user_ids:
            if not user_id:
                continue
            names.append(self.user_name(user_id) or user_id)
        return names

    def resolve_mentions(self, text: str) -> str:
        """본문 안의 `<@U…>` 멘션과 `<#C…|>` 채널 링크를 이름으로 바꾼다. 스레드 첫 메시지가
        제목 재료로 쓰이므로(`storage/document_title.py`), 멘션이 ID로 남아 있으면 제목에
        그대로 박힌다."""

        def _user(match: "re.Match[str]") -> str:
            return self.user_name(match.group(1)) or match.group(0)

        def _channel(match: "re.Match[str]") -> str:
            name = self.channel_name(match.group(1))
            return f"#{name}" if name else match.group(0)

        text = _MENTION_RE.sub(_user, text)
        return _CHANNEL_LINK_RE.sub(_channel, text)

    def _load_users(self) -> dict[str, str | None]:
        if self._users is not None:
            return self._users
        users: dict[str, str | None] = {}
        cursor: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {"limit": _USERS_PAGE_SIZE}
                if cursor:
                    kwargs["cursor"] = cursor
                response = self._client.users_list(**kwargs)
                for member in response.get("members") or []:
                    user_id = member.get("id")
                    if user_id:
                        users[user_id] = _display_name(member)
                cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
                if not cursor:
                    break
        except Exception as exc:  # noqa: BLE001 - 위 docstring 참고
            logger.warning("slack users.list failed (participant names stay raw ids): %s", exc)
        self._users = users
        return users


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
        directory: "SlackDirectory | None" = None,
    ):
        """`directory`(SlackDirectory)를 주면 채널 ID/유저 ID를 사람이 읽는 이름으로 풀어
        `metadata.channel_name`/`metadata.user_name`에 **덧붙인다**. 원시 `channel`/`user`는
        그대로 남는다 - 그 둘은 `context/slack.py`의 스레드 그룹핑 키이자
        `postgres_store.find_slack_thread_*`/`document_links.py`의 조인 키라, 값이 바뀌면
        조용히 스레드가 갈라진다. 안 주면 예전과 완전히 같은 메타데이터가 나온다.

        `channel_project_map` (channel id -> project_id) is optional - when
        given, it lets one SlackCollector instance poll channels that belong
        to different projects and still tag each RawItem with the right
        project_id (see RawItem.project_id / pipeline.run_pipeline). A
        channel missing from the map falls back to the run-level project_id,
        same as when no map is given at all.
        """
        super().__init__(client=client, auth_config=auth_config)
        self._channels = channels
        self._channel_project_map = channel_project_map or {}
        self._directory = directory
        self._latest_ts: dict[str, str] = {}

    def _track_latest(self, channel: str, ts: str) -> None:
        current = self._latest_ts.get(channel)
        if current is None or float(ts) > float(current):
            self._latest_ts[channel] = ts

    def _make_item(self, channel: str, message: dict) -> RawItem:
        ts = message["ts"]
        text = message.get("text", "")
        data = text.encode("utf-8")
        metadata: dict[str, Any] = {
            "channel": channel,
            "ts": ts,
            "user": message.get("user"),
            "thread_ts": message.get("thread_ts"),
        }
        if self._directory is not None:
            # 원시 ID 옆에 이름을 **추가**만 한다(위 __init__ docstring 참고). 못 풀면 키를
            # 아예 넣지 않는다 - 빈 문자열/원시 ID를 이름 자리에 넣으면 아래 단계가 "풀렸다"고
            # 착각한다.
            channel_name = self._directory.channel_name(channel)
            if channel_name:
                metadata["channel_name"] = channel_name
            user_name = self._directory.user_name(message.get("user"))
            if user_name:
                metadata["user_name"] = user_name
            # 본문의 `<@U…>` 멘션은 여기서 이름으로 풀어 `text`에 실어 둔다. `content`(=검색
            # 대상 원문)는 건드리지 않는다 - 그걸 바꾸면 content_hash가 바뀌어 기존 문서가
            # 통째로 새 document_id로 재적재된다.
            if text:
                metadata["text_resolved"] = self._directory.resolve_mentions(text)
        return RawItem(
            uri=f"slack://{channel}/{ts}",
            content=data,
            content_hash=compute_content_hash(data),
            modified_at=_ts_to_datetime(ts),
            metadata=metadata,
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
        # Seed _latest_ts from the incoming cursor before syncing - a
        # channel with zero new messages this round never calls
        # _track_latest, so without this its prior position would silently
        # drop out of get_cursor()'s result (and a persisted cursor save,
        # see ingest.py's run_saas_ingest, would then forget it - the next
        # sync would re-pull that channel's entire history from scratch).
        self._latest_ts = dict(channel_cursors)
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
