"""`PostgresStore.backfill_slack_display_names` 통합 테스트.

`KF_TEST_DATABASE_URL`이 없으면 건너뛴다. **공유 개발 DB를 쓰지 마라** - 이 테스트는
슬랙 소스/문서를 만들고 지운다. 스크래치 DB를 따로 만들어 쓴다:

    docker exec -e PGPASSWORD=kf knowledge-forge-postgres-1 createdb -U kf kf_scratch_slack
    KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/kf_scratch_slack \
        uv run --extra dev pytest tests/storage/test_slack_name_backfill.py
"""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb

from knowledge_forge.storage.postgres_store import PostgresStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

_AT = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)


class _FakeDirectory:
    """`SlackDirectory`가 노출하는 세 메서드만 흉내낸다 - 백필은 프로토콜만 받으므로
    이 테스트는 Slack SDK도 네트워크도 필요 없다."""

    def __init__(self, channels=None, users=None):
        self._channels = channels or {}
        self._users = users or {}
        self.channel_calls: list[str] = []

    def channel_name(self, channel_id):
        self.channel_calls.append(channel_id)
        return self._channels.get(channel_id)

    def user_names(self, user_ids):
        return [self._users.get(u) or u for u in user_ids if u]

    def resolve_mentions(self, text):
        for user_id, name in self._users.items():
            text = text.replace(f"<@{user_id}>", name)
        return text


@pytest_asyncio.fixture
async def store():
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        yield db
        await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-%'")
        await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-%'")


async def _seed(store, source_id, document_id, metadata, first_text, title):
    await store.conn.execute(
        "INSERT INTO sources (source_id, source_type, uri, content_hash, collected_at, modified_at, metadata) "
        "VALUES (%s, 'slack', %s, 'h', %s, %s, %s)",
        (source_id, f"slack://{source_id}", _AT, _AT, Jsonb(metadata)),
    )
    elements = [
        {"type": "heading", "element_id": "h", "level": 1, "text": "Slack #C1 스레드"},
        {"type": "text", "element_id": "t", "text": first_text},
    ]
    await store.conn.execute(
        "INSERT INTO documents (document_id, source_id, elements, metadata, title) VALUES (%s, %s, %s, %s, %s)",
        (document_id, source_id, Jsonb(elements), Jsonb({}), title),
    )


async def _read(store, source_id, document_id):
    async with store.conn.cursor() as cur:
        await cur.execute("SELECT metadata FROM sources WHERE source_id = %s", (source_id,))
        metadata = (await cur.fetchone())[0]
        await cur.execute("SELECT title FROM documents WHERE document_id = %s", (document_id,))
        title = (await cur.fetchone())[0]
    return metadata, title


@pytest.mark.asyncio
async def test_backfill_names_the_channel_and_rewrites_the_title_without_losing_raw_ids(store):
    await _seed(
        store,
        "test-s1",
        "test-d1",
        {"channel": "C1", "thread_ts": "1.0", "participants": ["U1", "U2"], "message_count": 2},
        "U0BDNDBHWLV: <@U1> 3차 인터뷰 체크리스트 전달드립니다",
        "#C1 2026-09-01",
    )
    directory = _FakeDirectory(channels={"C1": "proj_2026-038-0_nh농협은행_mvp"}, users={"U1": "샨", "U2": "딘"})

    stats = await store.backfill_slack_display_names(directory)

    metadata, title = await _read(store, "test-s1", "test-d1")
    assert stats["sources_updated"] == 1 and stats["titles_updated"] == 1
    # 292cb2d부터 제목에 첫 메시지 발췌(멘션은 표시명으로 해석)가 붙는다.
    assert title == "#proj_2026-038-0_nh농협은행_mvp 2026-09-01 샨 3차 인터뷰 체크리스트 전달드립니다"
    # 원시 기록은 그대로 - `channel`은 스레드 조회/링크 백필의 조인 키다.
    assert metadata["channel"] == "C1"
    assert metadata["participants"] == ["U1", "U2"]
    assert metadata["channel_name"] == "proj_2026-038-0_nh농협은행_mvp"
    assert metadata["participant_names"] == ["샨", "딘"]
    # 화자 접두어는 제목 유도(`slack_excerpt`)가 떼므로 원재료는 그대로 실어 둔다.
    assert metadata["first_message"] == "U0BDNDBHWLV: 샨 3차 인터뷰 체크리스트 전달드립니다"


@pytest.mark.asyncio
async def test_backfill_is_idempotent(store):
    await _seed(store, "test-s2", "test-d2", {"channel": "C1", "participants": ["U1"]}, "U0BDNDBHWLV: 안녕", "#C1 2026-09-01")
    directory = _FakeDirectory(channels={"C1": "general"}, users={"U1": "샨"})

    first = await store.backfill_slack_display_names(directory)
    second = await store.backfill_slack_display_names(directory)

    assert first["sources_updated"] == 1 and first["titles_updated"] == 1
    assert second == dict(first, sources_updated=0, titles_updated=0, channels_resolved=0,
                          participants_resolved=0, first_messages_filled=0)


@pytest.mark.asyncio
async def test_unresolvable_channel_is_counted_and_leaves_the_id_in_the_title(store):
    await _seed(store, "test-s3", "test-d3", {"channel": "CZZ", "participants": ["U9"]}, "U0BDNDBHWLV: 안녕", "#CZZ 2026-09-01")
    directory = _FakeDirectory(channels={}, users={})

    stats = await store.backfill_slack_display_names(directory)

    metadata, title = await _read(store, "test-s3", "test-d3")
    assert stats["channels_resolved"] == 0 and stats["channels_unresolved"] == 1
    assert "channel_name" not in metadata
    # 참여자를 하나도 못 풀었으면 `participant_names`를 만들지 않는다 - 원시 ID를 이름인 척
    # 넣어 두면 다음 실행이 "이미 채워졌다"고 건너뛴다.
    assert "participant_names" not in metadata
    assert title == "#CZZ 2026-09-01 안녕"


@pytest.mark.asyncio
async def test_backfill_touches_only_sources_metadata_and_documents_title(store):
    await _seed(store, "test-s4", "test-d4", {"channel": "C1", "participants": ["U1"]}, "U0BDNDBHWLV: 안녕", "#C1 2026-09-01")
    async with store.conn.cursor() as cur:
        await cur.execute("SELECT elements, metadata, updated_at FROM documents WHERE document_id = 'test-d4'")
        before = await cur.fetchone()

    await store.backfill_slack_display_names(_FakeDirectory(channels={"C1": "general"}, users={"U1": "샨"}))

    async with store.conn.cursor() as cur:
        await cur.execute("SELECT elements, metadata, updated_at FROM documents WHERE document_id = 'test-d4'")
        after = await cur.fetchone()
    assert after == before
