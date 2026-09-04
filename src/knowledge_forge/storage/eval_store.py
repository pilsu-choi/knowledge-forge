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
from pydantic import BaseModel, Field

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


class ChunkSample(BaseModel):
    """`eval-gen`이 질의 생성의 원천으로 뽑아 오는 원문 청크 한 개
    (wiki/search-evaluation.md (C)의 eval-gen). KnowledgeObject가 아니라
    `chunks.text`를 쓰는 이유는 그쪽 문서에 적혀 있다 - 추출 품질이 골든셋
    품질의 상한이 되는 걸 피하고, `--no-extract`로 인제스트한 코퍼스에서도
    돌아야 하기 때문이다.

    `content_hash`는 정답 문서의 `sources.content_hash` - `golden.py::
    compute_source_hash`에 그대로 넣어 `source_hash`를 채운다."""

    chunk_id: str
    document_id: str
    source_id: str
    source_type: str
    content_hash: str
    text: str
    token_count: int
    title: str | None = None
    uri: str = ""


class EvalRunQuery(BaseModel):
    """One golden query's retrieval outcome inside an `EvalRun`
    (`eval_run_query` row). `metrics` is empty and `error` set when
    retrieval raised - the run keeps going, the query just scores nothing."""

    run_id: str
    query_id: str
    query_text: str
    primary_type: str
    types: list[str]
    ranked_document_ids: list[str] = Field(default_factory=list)
    scores: list[float] = Field(default_factory=list)
    predicted_count: int | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    strategies: list[str] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None


class EvalRun(BaseModel):
    """One `knowledge-forge eval-ir` invocation (`eval_run` row). `queries`
    is not a column: the runner fills it so the CLI can print/serialize a
    run without a second round-trip, `get_eval_run` leaves it empty and
    `list_eval_run_queries` fetches the rows on demand."""

    run_id: str
    golden_version: str
    golden_path: str
    project_id: str | None = None
    config: dict = Field(default_factory=dict)
    config_hash: str
    index_snapshot: dict = Field(default_factory=dict)
    # 돌아간 코드의 식별자. 깨끗하면 커밋 sha 그대로, 수정된 작업 트리에서
    # 측정했으면 `<sha>+dirty.<12hex>` (evaluation/ir_runner.py의 긴 주석 참고 -
    # b470057 하나에 0.736과 0.477이 같이 적혀 버린 사고가 이 표기의 이유다).
    git_commit: str | None = None
    # dirty였던 run의 변경 파일 목록(`git status --porcelain` 줄). 깨끗한 run과
    # 이 컬럼 이전의 옛 행은 둘 다 None.
    dirty_files: list[str] | None = None
    label: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    summary: dict = Field(default_factory=dict)
    queries: list[EvalRunQuery] = Field(default_factory=list, exclude=True)


_LOG_COLUMNS = "log_id, project_id, query, answer, evidence, tool_calls, latency_ms, created_at"
_FEEDBACK_COLUMNS = "feedback_id, log_id, rating, comment, created_at"
_SCORE_COLUMNS = "log_id, groundedness_score, citation_precision, claims, judge_model, judged_at"
# dirty_files는 나중에 ALTER로 붙은 컬럼이라 맨 뒤 - 기존 행 인덱스(row[0..11])를
# 그대로 두려는 의도다.
_RUN_COLUMNS = (
    "run_id, golden_version, golden_path, project_id, config, config_hash, index_snapshot, "
    "git_commit, label, started_at, finished_at, summary, dirty_files"
)
_RUN_QUERY_COLUMNS = (
    "run_id, query_id, query_text, primary_type, types, ranked_document_ids, scores, predicted_count, "
    "metrics, strategies, steps, elapsed_ms, error"
)


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


def _row_to_run(row: tuple) -> EvalRun:
    return EvalRun(
        run_id=row[0],
        golden_version=row[1],
        golden_path=row[2],
        project_id=row[3],
        config=row[4],
        config_hash=row[5],
        index_snapshot=row[6],
        git_commit=row[7],
        label=row[8],
        started_at=row[9],
        finished_at=row[10],
        summary=row[11],
        dirty_files=row[12],
    )


def _row_to_run_query(row: tuple) -> EvalRunQuery:
    return EvalRunQuery(
        run_id=row[0],
        query_id=row[1],
        query_text=row[2],
        primary_type=row[3],
        types=row[4],
        ranked_document_ids=row[5],
        scores=row[6],
        predicted_count=row[7],
        metrics=row[8],
        strategies=row[9],
        steps=row[10],
        elapsed_ms=row[11],
        error=row[12],
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
        assert row is not None  # a single-row INSERT ... RETURNING always yields exactly one row
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
        assert row is not None  # a single-row INSERT ... RETURNING always yields exactly one row
        return _row_to_feedback(row)

    async def list_unscored_search_logs(self, project_id: str | None = None, limit: int = 20) -> list[SearchLog]:
        """`search_log` rows with no `eval_score` yet - what `knowledge-forge
        eval` (search-evaluation.md (B)) fetches to judge next."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
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
        assert row is not None  # an upsert's RETURNING always yields exactly one row
        return _row_to_score(row)

    async def get_eval_score(self, log_id: str) -> EvalScore | None:
        async with self.conn.cursor() as cur:
            await cur.execute(f"SELECT {_SCORE_COLUMNS} FROM eval_score WHERE log_id = %s", (log_id,))
            row = await cur.fetchone()
        return _row_to_score(row) if row is not None else None

    # --- Golden-set IR evaluation runs (search-evaluation.md (C)) ---------

    async def save_eval_run(self, run: EvalRun) -> EvalRun:
        """Upsert so the runner can write the row once at start (with an
        empty summary) and again at finish - a crashed run still leaves a
        trace with `finished_at IS NULL`. `run.queries` is not written here;
        see `save_eval_run_queries`."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO eval_run ({_RUN_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    config = EXCLUDED.config,
                    config_hash = EXCLUDED.config_hash,
                    index_snapshot = EXCLUDED.index_snapshot,
                    git_commit = EXCLUDED.git_commit,
                    dirty_files = EXCLUDED.dirty_files,
                    label = EXCLUDED.label,
                    finished_at = EXCLUDED.finished_at,
                    summary = EXCLUDED.summary
                RETURNING {_RUN_COLUMNS}
                """,
                (
                    run.run_id,
                    run.golden_version,
                    run.golden_path,
                    run.project_id,
                    Jsonb(run.config),
                    run.config_hash,
                    Jsonb(run.index_snapshot),
                    run.git_commit,
                    run.label,
                    run.started_at,
                    run.finished_at,
                    Jsonb(run.summary),
                    Jsonb(run.dirty_files) if run.dirty_files is not None else None,
                ),
            )
            row = await cur.fetchone()
        assert row is not None  # an upsert's RETURNING always yields exactly one row
        return _row_to_run(row)

    async def save_eval_run_queries(self, run_id: str, rows: list[EvalRunQuery]) -> int:
        if not rows:
            return 0
        async with self.conn.cursor() as cur:
            await cur.executemany(
                f"""
                INSERT INTO eval_run_query ({_RUN_QUERY_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, query_id) DO UPDATE SET
                    query_text = EXCLUDED.query_text,
                    primary_type = EXCLUDED.primary_type,
                    types = EXCLUDED.types,
                    ranked_document_ids = EXCLUDED.ranked_document_ids,
                    scores = EXCLUDED.scores,
                    predicted_count = EXCLUDED.predicted_count,
                    metrics = EXCLUDED.metrics,
                    strategies = EXCLUDED.strategies,
                    steps = EXCLUDED.steps,
                    elapsed_ms = EXCLUDED.elapsed_ms,
                    error = EXCLUDED.error
                """,
                [
                    (
                        run_id,
                        q.query_id,
                        q.query_text,
                        q.primary_type,
                        Jsonb(q.types),
                        Jsonb(q.ranked_document_ids),
                        Jsonb(q.scores),
                        q.predicted_count,
                        Jsonb(q.metrics),
                        Jsonb(q.strategies),
                        Jsonb(q.steps),
                        q.elapsed_ms,
                        q.error,
                    )
                    for q in rows
                ],
            )
        return len(rows)

    async def get_eval_run(self, run_id: str) -> EvalRun | None:
        async with self.conn.cursor() as cur:
            await cur.execute(f"SELECT {_RUN_COLUMNS} FROM eval_run WHERE run_id = %s", (run_id,))
            row = await cur.fetchone()
        return _row_to_run(row) if row is not None else None

    async def list_eval_runs(self, golden_version: str | None = None, limit: int = 20) -> list[EvalRun]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_RUN_COLUMNS} FROM eval_run
                WHERE (%s::text IS NULL OR golden_version = %s)
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (golden_version, golden_version, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_run(row) for row in rows]

    async def list_eval_run_queries(self, run_id: str) -> list[EvalRunQuery]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_RUN_QUERY_COLUMNS} FROM eval_run_query WHERE run_id = %s ORDER BY query_id",
                (run_id,),
            )
            rows = await cur.fetchall()
        return [_row_to_run_query(row) for row in rows]

    async def snapshot_index(self, project_id: str | None = None) -> dict:
        """Measured index size for `eval_run.index_snapshot` - counted at run
        time rather than copied from the gold file so a drifted index shows
        up as a snapshot mismatch in `eval-ir --compare`. chunks/knowledge_
        objects carry no project_id of their own, so they are scoped through
        their document."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM documents WHERE (%s::text IS NULL OR project_id = %s)",
                (project_id, project_id),
            )
            documents = (await cur.fetchone() or (0,))[0]
            await cur.execute(
                """
                SELECT count(*) FROM chunks c JOIN documents d ON d.document_id = c.document_id
                WHERE (%s::text IS NULL OR d.project_id = %s)
                """,
                (project_id, project_id),
            )
            chunks = (await cur.fetchone() or (0,))[0]
            await cur.execute(
                """
                SELECT count(*) FROM knowledge_objects k JOIN documents d ON d.document_id = k.document_id
                WHERE (%s::text IS NULL OR d.project_id = %s)
                """,
                (project_id, project_id),
            )
            knowledge_objects = (await cur.fetchone() or (0,))[0]
            await cur.execute(
                """
                SELECT target_kind, count(*) FROM knowledge_object_embeddings
                WHERE (%s::text IS NULL OR project_id = %s)
                GROUP BY target_kind ORDER BY target_kind
                """,
                (project_id, project_id),
            )
            by_kind = {kind: n for kind, n in await cur.fetchall()}
        return {
            "documents": documents,
            "chunks": chunks,
            "knowledge_objects": knowledge_objects,
            "embeddings_by_kind": by_kind,
        }

    async def content_hashes_for(self, document_ids: list[str]) -> dict[str, str]:
        """`{document_id: sources.content_hash}` for `golden.check_staleness`
        - ids missing from the index are simply absent from the result,
        which is exactly what the checker treats as `missing`."""
        if not document_ids:
            return {}
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, s.content_hash
                FROM documents d JOIN sources s ON s.source_id = d.source_id
                WHERE d.document_id = ANY(%s)
                """,
                (list(document_ids),),
            )
            rows = await cur.fetchall()
        return {document_id: content_hash for document_id, content_hash in rows}

    async def sources_by_type(self, project_id: str | None = None) -> dict[str, int]:
        """`{source_type: 개수}` - 골든셋 파일의 `index_snapshot.sources_by_type`을
        채운다. `snapshot_index`가 documents/chunks/knowledge_objects만 세는 것과
        달리 이건 층화 샘플링이 어느 소스에서 얼마나 뽑을 수 있는지를 보여주는 값이라
        `eval-gen` 쪽에서만 필요하다."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT source_type, count(*) FROM sources
                WHERE (%s::text IS NULL OR project_id = %s)
                GROUP BY source_type ORDER BY source_type
                """,
                (project_id, project_id),
            )
            return {source_type: n for source_type, n in await cur.fetchall()}

    async def list_chunk_candidates(
        self,
        *,
        project_id: str | None = None,
        min_token_count: int = 80,
        source_types: list[str] | None = None,
        limit: int = 5000,
    ) -> list[ChunkSample]:
        """`eval-gen`의 층화 샘플링 후보 풀. 층화 자체(소스 타입별 균등 배분,
        문서당 상한, 시드)는 순수 함수 `evaluation/gen.py::stratified_sample`이
        하고, 여기서는 **필터링된 후보를 결정적인 순서로** 돌려주기만 한다 -
        그래야 같은 시드로 같은 샘플이 재현된다(`ORDER BY chunk_id`, DB의
        기본 행 순서에 의존하지 않음).

        `min_token_count`는 청크 노이즈 필터다: 목차 한 줄, 서명 블록, 빈
        슬라이드 같은 짧은 청크로는 "이 청크로만 답할 수 있는 질문"이 나오지
        않는다."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT c.chunk_id, c.document_id, c.source_id, s.source_type, s.content_hash,
                       c.text, c.token_count, d.metadata->>'title', s.uri
                FROM chunks c
                JOIN documents d ON d.document_id = c.document_id
                JOIN sources s ON s.source_id = c.source_id
                WHERE c.token_count >= %s
                  AND (%s::text IS NULL OR d.project_id = %s)
                  AND (%s::text[] IS NULL OR s.source_type = ANY(%s))
                ORDER BY c.chunk_id
                LIMIT %s
                """,
                (min_token_count, project_id, project_id, source_types, source_types, limit),
            )
            rows = await cur.fetchall()
        return [
            ChunkSample(
                chunk_id=chunk_id,
                document_id=document_id,
                source_id=source_id,
                source_type=source_type,
                content_hash=content_hash,
                text=text,
                token_count=token_count,
                title=title,
                uri=uri or "",
            )
            for chunk_id, document_id, source_id, source_type, content_hash, text, token_count, title, uri in rows
        ]
