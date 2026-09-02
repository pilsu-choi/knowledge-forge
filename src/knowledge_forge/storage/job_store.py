"""Postgres-backed ingest job queue (wiki/api.md).

Deliberately a separate class from `PostgresStore`/`PipelineStore`: those
persist the Knowledge Graph itself, this persists queue *events* (a job's
lifecycle), and their callers differ (the FastAPI process enqueues, the
Celery-driven worker process runs and updates). Same connection pattern as
`PostgresStore` -
autocommit, one connection per instance, used as an async context manager -
for the same reason: simple lifecycle, self-heals across process restarts.
"""

import uuid
from datetime import datetime, timezone
from enum import Enum

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from knowledge_forge.storage.postgres_store import _SCHEMA_PATH


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(BaseModel):
    job_id: str
    job_type: str
    payload: dict
    status: JobStatus
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    project_id: str = "default"


_COLUMNS = "job_id, job_type, payload, status, result, error, created_at, started_at, finished_at, project_id"


def _row_to_job(row: tuple) -> Job:
    return Job(
        job_id=row[0],
        job_type=row[1],
        payload=row[2],
        status=row[3],
        result=row[4],
        error=row[5],
        created_at=row[6],
        started_at=row[7],
        finished_at=row[8],
        project_id=row[9],
    )


class JobStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> "JobStore":
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "JobStore":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def conn(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("JobStore is not connected - use it as a context manager or call connect()")
        return self._conn

    async def init_schema(self) -> None:
        # Same schema.sql as PostgresStore (CREATE TABLE IF NOT EXISTS
        # throughout) - safe to call from both without coordinating who
        # "owns" schema creation.
        await self.conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))

    async def enqueue(self, job_type: str, payload: dict, project_id: str = "default") -> Job:
        job_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO jobs (job_id, job_type, payload, status, created_at, project_id)
                VALUES (%s, %s, %s, 'pending', %s, %s)
                RETURNING {_COLUMNS}
                """,
                (job_id, job_type, Jsonb(payload), created_at, project_id),
            )
            row = await cur.fetchone()
        return _row_to_job(row)

    async def get(self, job_id: str) -> Job | None:
        async with self.conn.cursor() as cur:
            await cur.execute(f"SELECT {_COLUMNS} FROM jobs WHERE job_id = %s", (job_id,))
            row = await cur.fetchone()
        return _row_to_job(row) if row is not None else None

    async def list(
        self,
        status: JobStatus | None = None,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS} FROM jobs
                WHERE (%s::text IS NULL OR status = %s)
                  AND (%s::text IS NULL OR project_id = %s)
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (
                    status.value if status else None,
                    status.value if status else None,
                    project_id,
                    project_id,
                    limit,
                    offset,
                ),
            )
            rows = await cur.fetchall()
        return [_row_to_job(row) for row in rows]

    async def mark_running(self, job_id: str) -> None:
        # Called by worker.py's `_execute_job` when a Celery task picks up
        # the job - Celery/Redis (not `FOR UPDATE SKIP LOCKED`) now owns
        # delivering each job to exactly one worker, so this is a plain
        # UPDATE rather than the old claim_next()'s atomic claim-and-lock.
        await self.conn.execute(
            "UPDATE jobs SET status = 'running', started_at = %s WHERE job_id = %s",
            (datetime.now(timezone.utc), job_id),
        )

    async def mark_succeeded(self, job_id: str, result: dict) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = 'succeeded', result = %s, finished_at = %s WHERE job_id = %s",
            (Jsonb(result), datetime.now(timezone.utc), job_id),
        )

    async def mark_failed(self, job_id: str, error: str) -> None:
        await self.conn.execute(
            "UPDATE jobs SET status = 'failed', error = %s, finished_at = %s WHERE job_id = %s",
            (error, datetime.now(timezone.utc), job_id),
        )
