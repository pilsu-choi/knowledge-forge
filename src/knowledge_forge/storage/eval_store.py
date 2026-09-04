"""Postgres-backed Search Evaluation / Feedback Loop store (wiki/search-evaluation.md).

Deliberately a separate class from `PostgresStore`/`PipelineStore`, same
reasoning as `JobStore`: those persist the Knowledge Graph itself, this
persists *event* rows (a search turn's log, a user's feedback on it, a
judge's score for it) written by a different caller (the agent runtime,
the `feedback`/`eval` CLI commands) on a different lifecycle. Same
connection pattern as `JobStore`/`PostgresStore` - autocommit, one
connection per instance, used as an async context manager.
"""

import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from knowledge_forge.storage.postgres_store import _SCHEMA_PATH


class SearchLog(BaseModel):
    log_id: str
    project_id: str = "default"
    query: str
    answer: str
    evidence: list[dict]
    tool_calls: list[dict]
    latency_ms: int
    created_at: datetime


class SearchFeedback(BaseModel):
    feedback_id: str
    log_id: str
    rating: str
    comment: str | None = None
    created_at: datetime


class EvalScore(BaseModel):
    log_id: str
    groundedness_score: float
    citation_precision: float | None = None
    claims: list[dict]
    judge_model: str
    judged_at: datetime


_LOG_COLUMNS = "log_id, project_id, query, answer, evidence, tool_calls, latency_ms, created_at"
_FEEDBACK_COLUMNS = "feedback_id, log_id, rating, comment, created_at"
_SCORE_COLUMNS = "log_id, groundedness_score, citation_precision, claims, judge_model, judged_at"


def _row_to_log(row: tuple) -> SearchLog:
    return SearchLog(
        log_id=row[0],
        project_id=row[1],
        query=row[2],
        answer=row[3],
        evidence=row[4],
        tool_calls=row[5],
        latency_ms=row[6],
        created_at=row[7],
    )


def _row_to_feedback(row: tuple) -> SearchFeedback:
    return SearchFeedback(feedback_id=row[0], log_id=row[1], rating=row[2], comment=row[3], created_at=row[4])


def _row_to_score(row: tuple) -> EvalScore:
    return EvalScore(
        log_id=row[0], groundedness_score=row[1], citation_precision=row[2], claims=row[3], judge_model=row[4], judged_at=row[5]
    )


class EvalStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> "EvalStore":
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "EvalStore":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def conn(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("EvalStore is not connected - use it as a context manager or call connect()")
        return self._conn

    async def init_schema(self) -> None:
        # Same schema.sql as PostgresStore/JobStore (CREATE TABLE IF NOT
        # EXISTS throughout) - safe to call from any of the three without
        # coordinating who "owns" schema creation.
        await self.conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))

    async def log_search(
        self,
        query: str,
        answer: str,
        evidence: list[dict],
        tool_calls: list[dict],
        latency_ms: int,
        project_id: str = "default",
    ) -> SearchLog:
        log_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO search_log (log_id, project_id, query, answer, evidence, tool_calls, latency_ms, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_LOG_COLUMNS}
                """,
                (log_id, project_id, query, answer, Jsonb(evidence), Jsonb(tool_calls), latency_ms, created_at),
            )
            row = await cur.fetchone()
        return _row_to_log(row)

    async def get_search_log(self, log_id: str) -> SearchLog | None:
        async with self.conn.cursor() as cur:
            await cur.execute(f"SELECT {_LOG_COLUMNS} FROM search_log WHERE log_id = %s", (log_id,))
            row = await cur.fetchone()
        return _row_to_log(row) if row is not None else None

    async def list_search_logs(
        self, project_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[SearchLog]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_LOG_COLUMNS} FROM search_log
                WHERE (%s::text IS NULL OR project_id = %s)
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (project_id, project_id, limit, offset),
            )
            rows = await cur.fetchall()
        return [_row_to_log(row) for row in rows]

    async def add_feedback(self, log_id: str, rating: str, comment: str | None = None) -> SearchFeedback:
        feedback_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO search_feedback (feedback_id, log_id, rating, comment, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING {_FEEDBACK_COLUMNS}
                """,
                (feedback_id, log_id, rating, comment, created_at),
            )
            row = await cur.fetchone()
        return _row_to_feedback(row)

    async def list_unscored_search_logs(self, project_id: str | None = None, limit: int = 20) -> list[SearchLog]:
        """`search_log` rows with no `eval_score` yet - what `knowledge-forge
        eval` (search-evaluation.md (B)) fetches to judge next."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT sl.log_id, sl.project_id, sl.query, sl.answer, sl.evidence, sl.tool_calls,
                       sl.latency_ms, sl.created_at
                FROM search_log sl
                LEFT JOIN eval_score es ON es.log_id = sl.log_id
                WHERE es.log_id IS NULL
                  AND (%s::text IS NULL OR sl.project_id = %s)
                ORDER BY sl.created_at ASC
                LIMIT %s
                """,
                (project_id, project_id, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_log(row) for row in rows]

    async def save_eval_score(
        self,
        log_id: str,
        groundedness_score: float,
        citation_precision: float | None,
        claims: list[dict],
        judge_model: str,
    ) -> EvalScore:
        judged_at = datetime.now(timezone.utc)
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO eval_score (log_id, groundedness_score, citation_precision, claims, judge_model, judged_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (log_id) DO UPDATE SET
                    groundedness_score = EXCLUDED.groundedness_score,
                    citation_precision = EXCLUDED.citation_precision,
                    claims = EXCLUDED.claims,
                    judge_model = EXCLUDED.judge_model,
                    judged_at = EXCLUDED.judged_at
                RETURNING {_SCORE_COLUMNS}
                """,
                (log_id, groundedness_score, citation_precision, Jsonb(claims), judge_model, judged_at),
            )
            row = await cur.fetchone()
        return _row_to_score(row)

    async def get_eval_score(self, log_id: str) -> EvalScore | None:
        async with self.conn.cursor() as cur:
            await cur.execute(f"SELECT {_SCORE_COLUMNS} FROM eval_score WHERE log_id = %s", (log_id,))
            row = await cur.fetchone()
        return _row_to_score(row) if row is not None else None
