"""`PostgresStore`의 읽기 전용 커넥션 풀(`_pooled_conn`) 검증 - KF_TEST_DATABASE_URL이
없으면 스킵(다른 storage 테스트와 같은 규약).

왜 이 파일이 있나: 풀을 넣으면서 가장 깨지기 쉬운 지점이 **세션 GUC**다.
`hnsw.iterative_scan = strict_order`와 pgvector 타입 등록은 둘 다 커넥션 스코프라,
풀에서 새로 열린 커넥션이 그걸 못 받으면 `search_by_embedding`이 **에러 없이** 히트를
잃는다(그 메서드의 docstring에 실측 사례). 그래서 (1) 풀 커넥션마다 GUC가 실제로 서 있는지,
(2) 풀 커넥션이 진짜로 동시 실행되는지(= 커넥션 하나였으면 성립하지 않던 것)를 확인한다.
"""

import asyncio
import os
from contextlib import AsyncExitStack

import pytest

from knowledge_forge.storage.postgres_store import _READ_POOL_MIN_SIZE, PostgresStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")


async def test_every_pooled_connection_gets_the_ann_scan_guc():
    """풀에 있는 커넥션을 **전부 동시에 붙잡은 채로** 확인한다 - 하나씩 빌리고 반납하면
    같은 커넥션을 계속 다시 받을 뿐이라 '설정된 커넥션 하나 + 설정 안 된 나머지'를 못 잡는다."""
    async with PostgresStore(DSN) as store:
        pool = await store._ensure_read_pool()
        assert pool is not None, "풀이 열려야 이 테스트가 의미가 있다"

        values: list[str] = []
        async with AsyncExitStack() as stack:
            conns = [await stack.enter_async_context(store._pooled_conn()) for _ in range(_READ_POOL_MIN_SIZE)]
            assert len({id(c) for c in conns}) == _READ_POOL_MIN_SIZE, "서로 다른 커넥션이어야 한다"
            for conn in conns:
                cur = await conn.execute("SHOW hnsw.iterative_scan")
                row = await cur.fetchone()
                assert row is not None
                values.append(str(row[0]))

    assert values == ["strict_order"] * _READ_POOL_MIN_SIZE


async def test_pooled_reads_actually_overlap():
    """`pg_sleep`으로 커넥션 하나면 불가능한 것만 본다 - 0.3초 쿼리 4개가 직렬이면
    1.2초, 풀이 있으면 0.3초대. psycopg3는 커넥션 하나에 대한 동시 실행을 내부 락으로
    직렬화하므로, 이 단축은 정확히 '커넥션이 여러 개'라는 뜻이다."""
    async with PostgresStore(DSN) as store:
        assert await store._ensure_read_pool() is not None

        async def _sleep() -> None:
            async with store._pooled_conn() as conn:
                await conn.execute("SELECT pg_sleep(0.3)")

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(*(_sleep() for _ in range(4)))
        elapsed = loop.time() - started

    assert elapsed < 0.9, f"읽기가 직렬화되고 있다 ({elapsed:.2f}s) - 풀이 안 쓰이는 것"


async def test_falls_back_to_the_shared_connection_before_connect():
    """`connect()` 전에는 풀도 만들지 않는다 - `self.conn`이 던지는 것과 같은 계약이라
    풀이 그걸 우회해 조용히 붙어 버리면 안 된다."""
    store = PostgresStore(DSN)

    assert await store._ensure_read_pool() is None
