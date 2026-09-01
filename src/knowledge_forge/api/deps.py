"""Per-request store dependencies.

Deliberately opens a fresh connection per request instead of holding a
pool: mirrors `scheduling/scheduler.py`'s "each tick opens its own store
connection" choice (simpler lifecycle, self-heals if Postgres drops a
connection between requests). A connection pool (`psycopg_pool`) is the
natural next step if this ever becomes a throughput bottleneck - not needed
for the request volumes this server is built for today.
"""

from typing import AsyncIterator

from fastapi import HTTPException

from knowledge_forge.config import settings
from knowledge_forge.storage.job_store import JobStore
from knowledge_forge.storage.postgres_store import PostgresStore

_NO_DB_DETAIL = "KF_DATABASE_URL이 설정되지 않았습니다 - 이 API는 Postgres 없이는 동작하지 않습니다."


def _require_database_url() -> str:
    if not settings.database_url:
        raise HTTPException(status_code=503, detail=_NO_DB_DETAIL)
    return settings.database_url


async def get_job_store() -> AsyncIterator[JobStore]:
    # Schema is initialized once at app startup (api/app.py's lifespan), not
    # per request - CREATE TABLE IF NOT EXISTS is idempotent but there is no
    # reason to re-run it on every call.
    dsn = _require_database_url()
    async with JobStore(dsn) as store:
        yield store


async def get_read_store() -> AsyncIterator[PostgresStore]:
    dsn = _require_database_url()
    async with PostgresStore(dsn) as store:
        yield store
