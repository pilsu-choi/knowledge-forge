"""PostgreSQL persistence for Source/Document/Chunk/KnowledgeObject.

Every table's primary key is a deterministic stable_id (knowledge_forge.ids),
so every write here is an idempotent upsert - re-running the pipeline on
unchanged input is safe to repeat.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from pgvector import Vector
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import (
    DocumentRef,
    Evidence,
    KnowledgeObject,
)
from knowledge_forge.models.link import DocumentLink
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source, SourceRef
from knowledge_forge.storage.document_title import derive_document_title, first_heading_text
from knowledge_forge.text_normalize import to_nfc

if TYPE_CHECKING:
    from knowledge_forge.embedding.base import EmbeddingRecord

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# How much of a chunk-grain embedding's `embedded_text` a vector_search hit
# carries as its `summary` (see `search_by_embedding`). A chunk can be up to
# `--max-tokens` (4000 by default) long, and one tool_result holding `limit`
# of those would blow the agent's context budget the same way an unclamped
# `limit` would (agent/tools.py::_MAX_SEARCH_LIMIT) - the agent can
# `read_file` the resource for the full text once it has picked a hit.
_CHUNK_HIT_SUMMARY_CHARS = 500

# 읽기 전용 검색 경로(`_read_conn`)가 쓰는 커넥션 풀 크기.
#
# 왜 풀이 필요한가: psycopg3의 async 커넥션은 커넥션 하나에 대한 동시 실행을 내부 락으로
# 직렬화한다. 그래서 `search/retrieve.py`의 arm들을 asyncio.gather로 겹쳐도 커넥션이
# 하나면 아무 이득이 없다 - 실측(이 코퍼스, 936청크): lexical_search 5회가 순차 182ms,
# 같은 커넥션에 gather 183ms(= 이득 0), 커넥션 5개로 gather 46ms(약 4배).
#
# max 8인 이유: 평가 하네스가 질의 4개를 동시에 돌리고(`eval-ir --concurrency 4`) 질의
# 하나가 arm 안에서 2~3개를 겹치므로 상한이 대략 그 곱이다. 그 위로 더 열어도 Postgres가
# 같은 코어에서 같은 일을 하는 것이라 처리량이 늘지 않는다.
#
# min 4인 이유: 풀이 필요할 때만 늘어나면 첫 버스트가 커넥션 하나를 놓고 줄을 선다
# (실측: 같은 gather 5회가 min=1이면 93ms, min=4면 57ms). 풀 자체가 **첫 검색 때** 열리므로
# (아래 `_read_conn` 참고) 검색을 한 번도 안 하는 소비자(적재 파이프라인, 워커, 스케줄러)는
# 이 4개를 아예 열지 않는다.
_READ_POOL_MIN_SIZE = 4
_READ_POOL_MAX_SIZE = 8


class PostgresStore:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None
        # `execute_readonly_query` opens an explicit transaction on the single
        # shared connection. Two coroutines doing that at once interleave
        # BEGIN/ROLLBACK and psycopg raises OutOfOrderTransactionNesting, so
        # that one method serializes on this lock. Everything else on the
        # store runs in autocommit and needs no lock.
        self._tx_lock = asyncio.Lock()
        # 읽기 전용 검색용 풀. `connect()`가 아니라 첫 검색 때 만들어진다(`_read_conn`).
        self._read_pool: AsyncConnectionPool | None = None
        self._read_pool_lock = asyncio.Lock()

    async def connect(self) -> "PostgresStore":
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        await self._prepare_connection(self.conn)
        return self

    async def _prepare_connection(self, conn: psycopg.AsyncConnection) -> None:
        """커넥션 하나를 이 코드베이스가 기대하는 상태로 만든다 - pgvector 타입 등록 +
        `hnsw.iterative_scan` 세션 GUC.

        **커넥션마다 반드시 다시 해야 하는 일이다.** 둘 다 세션 스코프라 새로 연 커넥션은
        아무것도 물려받지 않는다. 특히 `_configure_ann_search()`를 빠뜨린 커넥션에서
        `search_by_embedding`을 돌리면 에러 없이 히트를 잃는다(그 메서드의 docstring에
        실측 사례가 있다: 필터가 소수 행을 고르면 0히트) - 풀을 도입하면서 가장 깨지기
        쉬운 지점이 정확히 여기라서, `connect()`와 풀의 `configure=` 훅이 **같은 함수**를
        부르게 묶어 둔다. 한쪽만 고치는 실수가 나올 수 없게
        (`tests/storage/test_read_pool.py`가 풀 커넥션 전부를 동시에 붙잡고 확인한다)."""
        await self._register_vector_types(conn)
        await self._configure_ann_search(conn)

    async def _register_vector_types(self, conn: psycopg.AsyncConnection | None = None) -> None:
        """Teaches psycopg how to adapt `pgvector.Vector`/`numpy.ndarray`
        to/from the `vector` column type - required before any
        embedding-column read/write. Tolerates the `vector` extension not
        existing yet (a brand new database, before `init_schema()` has ever
        run) rather than raising: `connect()` itself must succeed against a
        fresh database so `init_schema()` can be reached at all to create
        the extension in the first place - this would otherwise be a
        chicken-and-egg failure on every first-time `knowledge-forge db init`.
        `init_schema()` calls this again once the extension actually exists."""
        from pgvector.psycopg import register_vector_async

        try:
            await register_vector_async(conn if conn is not None else self.conn)
        except psycopg.ProgrammingError:
            pass

    async def _configure_ann_search(self, conn: psycopg.AsyncConnection | None = None) -> None:
        """Turns pgvector's iterative index scan on for this session - without
        it `search_by_embedding` silently loses rows whenever a filter
        (project_id / target_kind / embedding_model / since-until) selects a
        minority of `knowledge_object_embeddings`.

        Why: an HNSW index scan walks the graph in distance order and hands
        the executor roughly `hnsw.ef_search` (40) rows; the WHERE clause is
        applied *after* that, and the scan then reports "no more rows"
        instead of walking further. So a query scoped to a small project can
        come back with zero hits even though matching rows exist - measured
        on this schema: 4000 rows in one project + 3 rows in another,
        `search_by_embedding(..., project_id=<the small one>)` returned 0
        with the default `hnsw.iterative_scan = off` and the correct 3 with
        this setting (see wiki/search-architecture.md "벡터 경로"). It is not
        a hypothetical: the executor filters by project_id on every call, and
        `search/temporal.py` adds since/until on top.

        `strict_order` rather than `relaxed_order`: the rows must come back in
        exact distance order, because `search_by_embedding` turns the
        distance into the `score` every caller ranks on. Work stays bounded by
        pgvector's own `hnsw.max_scan_tuples` (20000 by default), and an
        unfiltered query is unaffected (measured: same latency either way).

        Tolerates the GUC not existing (pgvector < 0.8) the same way
        `_register_vector_types` tolerates the extension not existing - a
        missing knob degrades to today's behaviour, it must not make
        `connect()` fail."""
        target = conn if conn is not None else self.conn
        try:
            await target.execute("SET hnsw.iterative_scan = strict_order")
        except psycopg.Error:  # pragma: no cover - only on pgvector < 0.8
            logger.warning("hnsw.iterative_scan is unavailable - filtered vector search may lose hits")

    @asynccontextmanager
    async def _read_conn(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """읽기 전용 검색 한 건이 쓸 커넥션을 빌려준다 - 풀에서 하나, 풀을 못 열면
        기존의 공유 커넥션.

        검색 메서드(`search_by_embedding` / `search_documents_by_title` /
        `search_chunks_lexical` / `execute_readonly_query`)만 이걸 쓴다. 쓰기 경로는 전부
        `self.conn` 그대로다 - upsert/트랜잭션이 어느 커넥션에서 도는지가 의미를 갖는
        곳이고, 거기서 동시성을 늘려 얻을 것도 없기 때문이다(파이프라인은 문서 단위로
        순차 실행된다).

        풀은 `connect()`가 아니라 여기서 지연 생성한다. 이유 두 가지:
        (1) 검색을 하지 않는 소비자(적재 CLI, Celery 워커, 스케줄러)가 쓰지도 않을
            커넥션을 열지 않는다.
        (2) 빈 데이터베이스에 처음 붙을 때는 `vector` 확장이 아직 없어서 타입 등록이
            no-op이 된다(`_register_vector_types`의 docstring). `init_schema()`가 확장을
            만든 **뒤**에 풀이 열리므로 풀 커넥션은 항상 등록에 성공한다.

        풀을 여는 데 실패하면(커넥션 상한, psycopg_pool 문제 등) 예외를 올리지 않고
        공유 커넥션으로 조용히 되돌아간다 - 이건 성능 최적화지 기능이 아니라서, 못 여는
        상황에서 검색이 아예 안 되는 것보다 느리게라도 되는 게 맞다."""
        pool = await self._ensure_read_pool()
        if pool is None:
            yield self.conn
            return
        async with pool.connection() as conn:
            yield conn

    async def _ensure_read_pool(self) -> AsyncConnectionPool | None:
        if self._read_pool is not None:
            return self._read_pool
        if self._conn is None:
            # connect() 전에는 풀도 만들지 않는다 - self.conn이 던지는 것과 같은 계약.
            return None
        async with self._read_pool_lock:
            if self._read_pool is not None:
                return self._read_pool
            pool = AsyncConnectionPool(
                self._dsn,
                min_size=_READ_POOL_MIN_SIZE,
                max_size=_READ_POOL_MAX_SIZE,
                # autocommit=True로 공유 커넥션과 세션 설정을 맞춘다 - 안 맞추면 단순
                # SELECT마다 암묵 트랜잭션이 열려 반납 때 ROLLBACK 왕복이 하나 더 붙고,
                # `execute_readonly_query`의 명시적 transaction() 블록 의미도 달라진다.
                kwargs={"autocommit": True},
                configure=self._prepare_connection,
                open=False,
            )
            try:
                await pool.open(wait=True, timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - 풀은 최적화지 기능이 아니다
                logger.warning("read connection pool unavailable, falling back to the shared connection: %s", exc)
                await pool.close()
                return None
            self._read_pool = pool
            return pool

    async def close(self) -> None:
        if self._read_pool is not None:
            await self._read_pool.close()
            self._read_pool = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "PostgresStore":
        return await self.connect()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def conn(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("PostgresStore is not connected - use it as a context manager or call connect()")
        return self._conn

    async def init_schema(self) -> None:
        await self.conn.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # The schema above is what actually creates the `vector` extension
        # on a brand new database - `connect()`'s registration attempt was
        # a no-op if this is the first time this database has ever seen it,
        # so retry it now that the type genuinely exists.
        await self._register_vector_types()
        await self._configure_ann_search()

    async def save_source(self, source: Source, raw_path: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO sources (source_id, source_type, uri, content_hash, raw_path,
                                  collected_at, modified_at, metadata, project_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                source_type = EXCLUDED.source_type,
                uri = EXCLUDED.uri,
                content_hash = EXCLUDED.content_hash,
                raw_path = EXCLUDED.raw_path,
                collected_at = EXCLUDED.collected_at,
                modified_at = EXCLUDED.modified_at,
                metadata = EXCLUDED.metadata,
                project_id = EXCLUDED.project_id
            """,
            (
                source.source_id,
                source.source_type.value,
                source.uri,
                source.content_hash,
                raw_path,
                source.collected_at,
                source.modified_at,
                Jsonb(source.metadata),
                source.project_id,
            ),
        )

    async def _derive_title_for(self, document: Document) -> str | None:
        """`documents.title` for `document` (storage/document_title.py). The
        `Document` model only carries a `SourceRef` (id/type/uri), not the
        source's metadata where the Drive file name / mail subject live, so
        this reads the already-saved `sources` row (pipeline.py always
        `record_source`s before `record_document`s). A missing source row
        isn't an error here - the FK on the INSERT will report it - it just
        means only the parser title / first heading are available."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                "SELECT source_type, metadata, COALESCE(modified_at, collected_at) FROM sources WHERE source_id = %s",
                (document.source.source_id,),
            )
            row = await cur.fetchone()
        return derive_document_title(
            metadata_title=document.metadata.title,
            source_type=row[0] if row else document.source.source_type.value,
            source_metadata=row[1] if row else None,
            content_at=row[2] if row else None,
            first_heading=first_heading_text(document.elements),
        )

    async def save_document(self, document: Document) -> None:
        now = datetime.now(timezone.utc)
        title = await self._derive_title_for(document)
        await self.conn.execute(
            """
            INSERT INTO documents (document_id, source_id, elements, metadata, project_id,
                                    created_at, updated_at, title)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (document_id) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                elements = EXCLUDED.elements,
                metadata = EXCLUDED.metadata,
                project_id = EXCLUDED.project_id,
                updated_at = EXCLUDED.updated_at,
                title = EXCLUDED.title
            """,
            (
                document.document_id,
                document.source.source_id,
                Jsonb([e.model_dump(mode="json") for e in document.elements]),
                Jsonb(document.metadata.model_dump(mode="json")),
                document.source.project_id,
                now,
                now,
                title,
            ),
        )

    async def list_sources(
        self, project_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Source]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT source_id, source_type, uri, content_hash, collected_at, modified_at,
                       metadata, project_id
                FROM sources
                WHERE (%s::text IS NULL OR project_id = %s)
                ORDER BY collected_at DESC
                LIMIT %s OFFSET %s
                """,
                (project_id, project_id, limit, offset),
            )
            rows = await cur.fetchall()
        return [
            Source(
                source_id=row[0],
                source_type=row[1],
                uri=row[2],
                content_hash=row[3],
                collected_at=row[4],
                modified_at=row[5],
                metadata=row[6],
                project_id=row[7],
            )
            for row in rows
        ]

    async def get_source(self, source_id: str) -> Source | None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT source_id, source_type, uri, content_hash, collected_at, modified_at,
                       metadata, project_id
                FROM sources
                WHERE source_id = %s
                """,
                (source_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Source(
            source_id=row[0],
            source_type=row[1],
            uri=row[2],
            content_hash=row[3],
            collected_at=row[4],
            modified_at=row[5],
            metadata=row[6],
            project_id=row[7],
        )

    async def list_documents(
        self,
        project_id: str | None = None,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.elements, d.metadata, d.created_at, d.updated_at,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE (%s::text IS NULL OR d.project_id = %s)
                  AND (%s::text IS NULL OR d.source_id = %s)
                ORDER BY d.document_id
                LIMIT %s OFFSET %s
                """,
                (project_id, project_id, source_id, source_id, limit, offset),
            )
            rows = await cur.fetchall()
        return [
            Document(
                document_id=row[0],
                elements=row[1],
                metadata=row[2],
                created_at=row[3],
                updated_at=row[4],
                source=SourceRef(source_id=row[5], source_type=row[6], uri=row[7], project_id=row[8]),
            )
            for row in rows
        ]

    async def get_document(self, document_id: str) -> Document | None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.elements, d.metadata, d.created_at, d.updated_at,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE d.document_id = %s
                """,
                (document_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Document(
            document_id=row[0],
            elements=row[1],
            metadata=row[2],
            created_at=row[3],
            updated_at=row[4],
            source=SourceRef(source_id=row[5], source_type=row[6], uri=row[7], project_id=row[8]),
        )

    async def save_chunks(self, chunks: list[Chunk]) -> None:
        """청크 본문은 NFC로 정규화해서 저장한다(`text_normalize.py`) - `chunks.text`는
        `search_chunks_lexical`의 ILIKE/trigram 대상이라, macOS·Drive 유래 NFD 파일명이
        본문에 인용돼 들어오면 NFC 질의로는 구조적으로 못 맞는다. 파서 출력 원본인
        `documents.elements`는 그대로 두고 여기서만 정규화한다 - 청크는 `element_ids`로
        엘리먼트를 참조하지 문자열로 맞대보지 않으므로 둘이 달라도 깨지는 곳이 없다.
        `token_count`는 재계산하지 않는다: NFC 합성은 코드포인트 수만 줄일 뿐이고,
        토큰 수는 어차피 청킹 시점의 근사치다."""
        now = datetime.now(timezone.utc)
        rows: list[tuple[Any, ...]] = [
            (
                chunk.chunk_id,
                chunk.document_id,
                chunk.source_id,
                chunk.hierarchy_level.value,
                chunk.sequence_index,
                chunk.parent_chunk_id,
                to_nfc(chunk.text),
                chunk.token_count,
                Jsonb(chunk.element_ids),
                chunk.page_range[0] if chunk.page_range else None,
                chunk.page_range[1] if chunk.page_range else None,
                now,
                now,
            )
            for chunk in chunks
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO chunks (chunk_id, document_id, source_id, hierarchy_level,
                                     sequence_index, parent_chunk_id, text, token_count,
                                     element_ids, page_start, page_end, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    source_id = EXCLUDED.source_id,
                    hierarchy_level = EXCLUDED.hierarchy_level,
                    sequence_index = EXCLUDED.sequence_index,
                    parent_chunk_id = EXCLUDED.parent_chunk_id,
                    text = EXCLUDED.text,
                    token_count = EXCLUDED.token_count,
                    element_ids = EXCLUDED.element_ids,
                    page_start = EXCLUDED.page_start,
                    page_end = EXCLUDED.page_end,
                    updated_at = EXCLUDED.updated_at
                """,
                rows,
            )

    async def list_chunks_by_document(
        self, document_id: str, *, hierarchy_level: HierarchyLevel | None = None
    ) -> list[Chunk]:
        """Every chunk of one document in reading order (`sequence_index`) -
        `search/document.py::read_document`'s single storage entry point.

        `hierarchy_level=None` (the default, and what `read_document` uses)
        returns every level together on purpose: `chunking/adaptive.py` only
        materializes *leaf* units - an ancestor that got split is never
        itself emitted (`parent_chunk_id` is always None today, and
        `(document_id, sequence_index)` is unique) - so one document's chunks
        across all levels are a single non-overlapping sequence, and
        filtering by level would silently drop text (the dev DB's two
        mixed-level documents are a paragraph+sentence email thread and a
        chapter+section report). The filter exists for callers that want a
        single grain (e.g. a future multi-resolution chunk store)."""
        level = hierarchy_level.value if hierarchy_level is not None else None
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT chunk_id, document_id, source_id, hierarchy_level, sequence_index,
                       parent_chunk_id, text, token_count, element_ids, page_start, page_end
                FROM chunks
                WHERE document_id = %s
                  AND (%s::text IS NULL OR hierarchy_level = %s)
                ORDER BY sequence_index
                """,
                (document_id, level, level),
            )
            rows = await cur.fetchall()
        return [
            Chunk(
                chunk_id=row[0],
                document_id=row[1],
                source_id=row[2],
                hierarchy_level=row[3],
                sequence_index=row[4],
                parent_chunk_id=row[5],
                text=row[6],
                token_count=row[7],
                element_ids=row[8],
                page_range=(row[9], row[10]) if row[9] is not None and row[10] is not None else None,
            )
            for row in rows
        ]

    async def save_knowledge_objects(self, objects: list[KnowledgeObject]) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                ko.knowledge_object_id,
                ko.type.value,
                ko.source.source_id,
                ko.document.document_id,
                Jsonb([e.model_dump(mode="json") for e in ko.evidence]),
                ko.summary,
                Jsonb(ko.content),
                Jsonb(ko.topics),
                ko.document_type,
                ko.extracted_at,
                ko.model_used,
                ko.version,
                ko.pipeline_version,
                ko.source_hash,
                ko.supersedes,
            )
            for ko in objects
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO knowledge_objects (knowledge_object_id, type, source_id, document_id,
                                                evidence, summary, content, topics, document_type,
                                                extracted_at, model_used, version, pipeline_version,
                                                source_hash, supersedes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (knowledge_object_id) DO UPDATE SET
                    type = EXCLUDED.type,
                    source_id = EXCLUDED.source_id,
                    document_id = EXCLUDED.document_id,
                    evidence = EXCLUDED.evidence,
                    summary = EXCLUDED.summary,
                    content = EXCLUDED.content,
                    topics = EXCLUDED.topics,
                    document_type = EXCLUDED.document_type,
                    extracted_at = EXCLUDED.extracted_at,
                    model_used = EXCLUDED.model_used,
                    version = EXCLUDED.version,
                    pipeline_version = EXCLUDED.pipeline_version,
                    source_hash = EXCLUDED.source_hash,
                    supersedes = EXCLUDED.supersedes
                """,
                rows,
            )

    async def list_knowledge_objects_by_source(self, source_id: str) -> list[KnowledgeObject]:
        """Every KnowledgeObject previously extracted from this `source_id`
        (across every prior document_id/content revision) - the candidate
        pool extraction/versioning.py matches a re-extraction's new objects
        against to auto-fill `supersedes`. Joins `sources` for
        `source_type`/`uri` since `knowledge_objects` only stores the id
        (see models/knowledge_object.py's DocumentRef/SourceRef note on why
        those stay lightweight refs)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE ko.source_id = %s
                """,
                (source_id,),
            )
            rows = await cur.fetchall()

        return [
            KnowledgeObject(
                knowledge_object_id=row[0],
                type=row[1],
                source=SourceRef(source_id=source_id, source_type=row[14], uri=row[15], project_id=row[16]),
                document=DocumentRef(document_id=row[2]),
                evidence=[Evidence(**e) for e in row[3]],
                summary=row[4],
                content=row[5],
                topics=row[6],
                document_type=row[7],
                extracted_at=row[8],
                model_used=row[9],
                version=row[10],
                pipeline_version=row[11],
                source_hash=row[12],
                supersedes=row[13],
            )
            for row in rows
        ]

    async def list_knowledge_objects(
        self, source_id: str | None = None, type: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[KnowledgeObject]:
        """General-purpose paginated listing for the read API (wiki/api.md),
        as opposed to `list_knowledge_objects_by_source` above which exists
        specifically for supersedes-matching. `type`/`source_id` are
        optional filters (%s::text IS NULL trick short-circuits an unset
        filter without a second SQL string)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE (%s::text IS NULL OR ko.source_id = %s)
                  AND (%s::text IS NULL OR ko.type = %s)
                ORDER BY ko.extracted_at DESC
                LIMIT %s OFFSET %s
                """,
                (source_id, source_id, type, type, limit, offset),
            )
            rows = await cur.fetchall()

        return [
            KnowledgeObject(
                knowledge_object_id=row[0],
                type=row[1],
                source=SourceRef(source_id=row[14], source_type=row[15], uri=row[16], project_id=row[17]),
                document=DocumentRef(document_id=row[2]),
                evidence=[Evidence(**e) for e in row[3]],
                summary=row[4],
                content=row[5],
                topics=row[6],
                document_type=row[7],
                extracted_at=row[8],
                model_used=row[9],
                version=row[10],
                pipeline_version=row[11],
                source_hash=row[12],
                supersedes=row[13],
            )
            for row in rows
        ]

    async def get_knowledge_object(self, knowledge_object_id: str) -> KnowledgeObject | None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE ko.knowledge_object_id = %s
                """,
                (knowledge_object_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return KnowledgeObject(
            knowledge_object_id=row[0],
            type=row[1],
            source=SourceRef(source_id=row[14], source_type=row[15], uri=row[16], project_id=row[17]),
            document=DocumentRef(document_id=row[2]),
            evidence=[Evidence(**e) for e in row[3]],
            summary=row[4],
            content=row[5],
            topics=row[6],
            document_type=row[7],
            extracted_at=row[8],
            model_used=row[9],
            version=row[10],
            pipeline_version=row[11],
            source_hash=row[12],
            supersedes=row[13],
        )

    async def list_relations(
        self,
        subject_id: str | None = None,
        object_id: str | None = None,
        type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Relation]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT relation_id, type, subject_id, object_id, label, confidence, evidence, created_at
                FROM relations
                WHERE (%s::text IS NULL OR subject_id = %s)
                  AND (%s::text IS NULL OR object_id = %s)
                  AND (%s::text IS NULL OR type = %s)
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (subject_id, subject_id, object_id, object_id, type, type, limit, offset),
            )
            rows = await cur.fetchall()
        return [
            Relation(
                relation_id=row[0],
                type=row[1],
                subject_id=row[2],
                object_id=row[3],
                label=row[4],
                confidence=row[5],
                evidence=row[6],
                created_at=row[7],
            )
            for row in rows
        ]

    async def save_relations(self, relations: list[Relation]) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                rel.relation_id,
                rel.type.value,
                rel.subject_id,
                rel.object_id,
                rel.label,
                rel.confidence,
                Jsonb(rel.evidence),
                rel.created_at,
            )
            for rel in relations
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO relations (relation_id, type, subject_id, object_id, label,
                                        confidence, evidence, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (relation_id) DO UPDATE SET
                    type = EXCLUDED.type,
                    subject_id = EXCLUDED.subject_id,
                    object_id = EXCLUDED.object_id,
                    label = EXCLUDED.label,
                    confidence = EXCLUDED.confidence,
                    evidence = EXCLUDED.evidence,
                    created_at = EXCLUDED.created_at
                """,
                rows,
            )

    async def get_sync_cursor(self, source: str) -> dict[str, Any] | None:
        """Returns the persisted `Cursor.data` for `source` (e.g. "slack"),
        or None when this source has never completed an incremental-sync run
        (ingest.py's run_saas_ingest then does a full initial_sync, same as
        before this table existed)."""
        async with self.conn.cursor() as cur:
            await cur.execute("SELECT cursor_data FROM sync_cursors WHERE source = %s", (source,))
            row = await cur.fetchone()
        return row[0] if row is not None else None

    async def save_sync_cursor(self, source: str, cursor_data: dict[str, Any], updated_at: datetime) -> None:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sync_cursors (source, cursor_data, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (source) DO UPDATE SET
                    cursor_data = EXCLUDED.cursor_data,
                    updated_at = EXCLUDED.updated_at
                """,
                (source, Jsonb(cursor_data), updated_at),
            )

    async def save_embeddings(self, records: list["EmbeddingRecord"]) -> None:
        """Upserts one `knowledge_object_embeddings` row per record, keyed
        by `EmbeddingRecord.embedding_id` (deterministic from target_kind +
        target id + model) - re-embedding the same KO/chunk with the same
        model replaces the row in place."""
        now = datetime.now(timezone.utc)
        rows: list[tuple[Any, ...]] = [
            (
                rec.embedding_id,
                rec.target_kind,
                rec.knowledge_object_id,
                rec.chunk_id,
                rec.document_id,
                rec.embedding_model,
                # register_vector_async() (connect(), above) only registers a
                # dumper for pgvector.Vector/numpy.ndarray, not a plain
                # list[float] - passed as-is, psycopg adapts it as a
                # double precision[] array instead of `vector`, and this
                # INSERT fails with "column is of type vector but expression
                # is of type double precision[]".
                Vector(rec.embedding),
                rec.embedded_text,
                rec.embedded_text_hash,
                rec.project_id,
                now,
            )
            for rec in records
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO knowledge_object_embeddings (embedding_id, target_kind, knowledge_object_id,
                                                           chunk_id, document_id, embedding_model,
                                                           embedding, embedded_text, embedded_text_hash,
                                                           project_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (embedding_id) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    embedded_text = EXCLUDED.embedded_text,
                    embedded_text_hash = EXCLUDED.embedded_text_hash,
                    document_id = EXCLUDED.document_id,
                    project_id = EXCLUDED.project_id,
                    created_at = EXCLUDED.created_at
                """,
                rows,
            )

    async def list_chunks_missing_embedding(
        self, embedding_model: str, project_id: str | None, limit: int
    ) -> list[tuple[Chunk, str]]:
        """Chunk-mode counterpart of `list_knowledge_objects_missing_embedding`
        (`KF_EMBEDDING_SOURCE=chunk`): chunks with no
        `knowledge_object_embeddings` row for `embedding_model` yet. Returns
        `(chunk, project_id)` pairs - `Chunk` itself carries no project_id,
        but the embedding row needs it (denormalized from the owning
        Source), so it rides along from the join. Ordered by
        `(document_id, sequence_index)` so a resumed backfill makes steady
        forward progress and one API batch tends to hold one document's
        neighbouring chunks."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT c.chunk_id, c.document_id, c.source_id, c.hierarchy_level, c.sequence_index,
                       c.parent_chunk_id, c.text, c.token_count, c.element_ids, c.page_start, c.page_end,
                       s.project_id
                FROM chunks c
                JOIN sources s ON s.source_id = c.source_id
                LEFT JOIN knowledge_object_embeddings e
                  ON e.chunk_id = c.chunk_id AND e.embedding_model = %s
                WHERE e.embedding_id IS NULL
                  AND (%s::text IS NULL OR s.project_id = %s)
                ORDER BY c.document_id, c.sequence_index
                LIMIT %s
                """,
                (embedding_model, project_id, project_id, limit),
            )
            rows = await cur.fetchall()

        return [
            (
                Chunk(
                    chunk_id=row[0],
                    document_id=row[1],
                    source_id=row[2],
                    hierarchy_level=row[3],
                    sequence_index=row[4],
                    parent_chunk_id=row[5],
                    text=row[6],
                    token_count=row[7],
                    element_ids=row[8],
                    page_range=(row[9], row[10]) if row[9] is not None and row[10] is not None else None,
                ),
                row[11],
            )
            for row in rows
        ]

    async def list_knowledge_objects_missing_embedding(
        self, embedding_model: str, project_id: str | None, limit: int
    ) -> list[KnowledgeObject]:
        """KnowledgeObjects that have no row yet in `knowledge_object_embeddings`
        for `embedding_model` - the backfill CLI command's work queue. Ordered
        by `extracted_at` so a resumed/repeated backfill run makes steady
        forward progress instead of re-scanning the same head of the table."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                LEFT JOIN knowledge_object_embeddings e
                  ON e.knowledge_object_id = ko.knowledge_object_id AND e.embedding_model = %s
                WHERE e.embedding_id IS NULL
                  AND (%s::text IS NULL OR s.project_id = %s)
                ORDER BY ko.extracted_at
                LIMIT %s
                """,
                (embedding_model, project_id, project_id, limit),
            )
            rows = await cur.fetchall()

        return [
            KnowledgeObject(
                knowledge_object_id=row[0],
                type=row[1],
                source=SourceRef(source_id=row[14], source_type=row[15], uri=row[16], project_id=row[17]),
                document=DocumentRef(document_id=row[2]),
                evidence=[Evidence(**e) for e in row[3]],
                summary=row[4],
                content=row[5],
                topics=row[6],
                document_type=row[7],
                extracted_at=row[8],
                model_used=row[9],
                version=row[10],
                pipeline_version=row[11],
                source_hash=row[12],
                supersedes=row[13],
            )
            for row in rows
        ]

    async def embedding_coverage(self, embedding_model: str, project_id: str | None = None) -> dict[str, Any]:
        """벡터 검색이 왜 0히트인지 사람이 30초 안에 판정하기 위한 읽기 전용 진단.

        벡터 경로가 조용히 실패하는 방식은 셋뿐인데(임베딩 행이 없다 / 모델 이름이 저장할
        때와 검색할 때 다르다 / 다른 프로젝트에만 있다) 셋 다 에러도 경고도 없이 "결과 없음"과
        구분되지 않는다(`search_by_embedding`의 docstring). 그래서 셋을 한 번에 보여준다:

        - `rows_by_model_and_kind`: **모델 필터를 걸지 않은** 실제 적재 현황
          `{embedding_model: {target_kind: n}}`. `embedding_model` 인자로 넘긴 이름이 여기
          없으면 그게 바로 원인이다 - 이름이 한 글자만 달라도 검색은 0행이다
          (`WHERE e.embedding_model = %s`).
        - `chunks_total`/`chunks_embedded`, `knowledge_objects_total`/`knowledge_objects_embedded`:
          그 모델 기준 grain별 커버리지(백필이 어디까지 갔는지).
        - `dimensions`: 실제로 저장된 벡터 차원 집합. 스키마가 `vector(1536)` 고정이라 다른
          차원은 애초에 INSERT가 실패하지만, 모델을 바꿨을 때 "왜 적재가 멈췄나"를 여기서 본다.

        `project_id`가 None이면 전체. 순수 SELECT라 `execute_readonly_query`와 달리 트랜잭션을
        열지 않는다(autocommit 그대로)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT embedding_model, target_kind, count(*)
                FROM knowledge_object_embeddings
                WHERE (%s::text IS NULL OR project_id = %s)
                GROUP BY embedding_model, target_kind
                ORDER BY embedding_model, target_kind
                """,
                (project_id, project_id),
            )
            by_model: dict[str, dict[str, int]] = {}
            for model, kind, n in await cur.fetchall():
                by_model.setdefault(model, {})[kind] = n

            await cur.execute(
                """
                SELECT count(*), count(e.embedding_id)
                FROM chunks c
                JOIN documents d ON d.document_id = c.document_id
                LEFT JOIN knowledge_object_embeddings e
                  ON e.chunk_id = c.chunk_id AND e.embedding_model = %s
                WHERE (%s::text IS NULL OR d.project_id = %s)
                """,
                (embedding_model, project_id, project_id),
            )
            chunks_total, chunks_embedded = await cur.fetchone() or (0, 0)

            await cur.execute(
                """
                SELECT count(*), count(e.embedding_id)
                FROM knowledge_objects ko
                JOIN documents d ON d.document_id = ko.document_id
                LEFT JOIN knowledge_object_embeddings e
                  ON e.knowledge_object_id = ko.knowledge_object_id AND e.embedding_model = %s
                WHERE (%s::text IS NULL OR d.project_id = %s)
                """,
                (embedding_model, project_id, project_id),
            )
            ko_total, ko_embedded = await cur.fetchone() or (0, 0)

            await cur.execute(
                """
                SELECT DISTINCT vector_dims(embedding)
                FROM knowledge_object_embeddings
                WHERE embedding_model = %s AND (%s::text IS NULL OR project_id = %s)
                """,
                (embedding_model, project_id, project_id),
            )
            dimensions = sorted(row[0] for row in await cur.fetchall())

        return {
            "embedding_model": embedding_model,
            "project_id": project_id,
            "rows_by_model_and_kind": by_model,
            "chunks_total": chunks_total,
            "chunks_embedded": chunks_embedded,
            "knowledge_objects_total": ko_total,
            "knowledge_objects_embedded": ko_embedded,
            "dimensions": dimensions,
        }

    async def search_by_embedding(
        self,
        embedding: list[float],
        embedding_model: str,
        project_id: str | None,
        type: str | None,
        limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
        target_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest-neighbor search over `knowledge_object_embeddings`' HNSW
        index (wiki/agent-search.md's vector_search) - `ORDER BY embedding
        <=> %s` (pgvector's cosine-distance operator, matching the index's
        `vector_cosine_ops`) `LIMIT %s`. `embedding` arrives as a plain
        `list[float]` but is wrapped in `pgvector.Vector` before being bound
        as a query parameter - `register_vector_async()` (`connect()`) only
        registers a dumper for `Vector`/`numpy.ndarray`, not a bare `list`,
        so an unwrapped list gets sent as a `double precision[]` array and
        `<=>` against `vector` fails with "operator does not exist" (the
        same reason `save_embeddings` wraps it on the INSERT side).

        Rows of both grains (`target_kind` 'knowledge_object' and 'chunk',
        see schema.sql) compete in one ranking unless `target_kind` narrows
        it - the two share a vector space (same model), so mixing them is
        sound; a caller measuring one grain in isolation (the evaluation
        golden set, an A/B of the two modes) passes it explicitly. A `type`
        filter only ever matches KO rows (chunks have no KO type), so
        `type=` implicitly excludes chunk hits.

        `since`/`until` (search/temporal.py, "⑥ Temporal Search") filter on
        `COALESCE(s.modified_at, s.collected_at)` - the source's own
        real-world timestamp, not `ko.extracted_at` (when the pipeline
        happened to process it). A batch extraction run stamps every
        KnowledgeObject it touches with the same `extracted_at` regardless
        of how old the underlying message actually is, so filtering on it
        would make a real "지난달" query return nothing no matter what's
        actually in scope (see search/service.py::_content_at's docstring
        for the same issue on the `file_search` side). `until` is exclusive,
        matching `search/temporal.py::TimeRange`'s half-open contract.

        Returns dict rows, not `KnowledgeObject`/`Chunk` - a search hit only
        needs enough to decide relevance plus a similarity `score`, not the
        full envelope. Every hit carries `target_kind`, `document_id`
        (both grains) and `knowledge_object_id`/`chunk_id` (one of them
        None). `summary` is the KO summary for KO hits and the embedded
        chunk text (whitespace-normalized, capped at `_CHUNK_HIT_SUMMARY_CHARS`)
        for chunk hits - one key so downstream (planner snippets, the agent
        reading the tool result) doesn't need to branch on grain; `type` is
        the KO type or the literal 'chunk'. `content_at` (the same COALESCE
        used for filtering) is included so a caller can see which timestamp
        the filter actually matched against."""
        vector = Vector(embedding)
        async with self._read_conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT e.target_kind, e.knowledge_object_id, e.chunk_id, e.document_id,
                       ko.type, ko.summary, ko.topics, ko.document_type,
                       LEFT(e.embedded_text, %s) AS chunk_text, c.hierarchy_level, c.sequence_index,
                       s.project_id, e.embedding <=> %s AS distance,
                       COALESCE(s.modified_at, s.collected_at) AS content_at
                FROM knowledge_object_embeddings e
                JOIN documents d ON d.document_id = e.document_id
                JOIN sources s ON s.source_id = d.source_id
                LEFT JOIN knowledge_objects ko ON ko.knowledge_object_id = e.knowledge_object_id
                LEFT JOIN chunks c ON c.chunk_id = e.chunk_id
                WHERE e.embedding_model = %s
                  AND (%s::text IS NULL OR e.target_kind = %s)
                  AND (%s::text IS NULL OR e.project_id = %s)
                  AND (%s::text IS NULL OR ko.type = %s)
                  AND (%s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) >= %s)
                  AND (%s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) < %s)
                ORDER BY e.embedding <=> %s
                LIMIT %s
                """,
                (
                    _CHUNK_HIT_SUMMARY_CHARS,
                    vector,
                    embedding_model,
                    target_kind,
                    target_kind,
                    project_id,
                    project_id,
                    type,
                    type,
                    since,
                    since,
                    until,
                    until,
                    vector,
                    limit,
                ),
            )
            rows = await cur.fetchall()

        hits: list[dict[str, Any]] = []
        for row in rows:
            kind = row[0]
            is_ko = kind == "knowledge_object"
            hits.append(
                {
                    "target_kind": kind,
                    "knowledge_object_id": row[1],
                    "chunk_id": row[2],
                    "document_id": row[3],
                    "type": row[4] if is_ko else "chunk",
                    "summary": row[5] if is_ko else row[8],
                    "topics": row[6] if is_ko else [],
                    "document_type": row[7],
                    "hierarchy_level": row[9],
                    "sequence_index": row[10],
                    "project_id": row[11],
                    # Cosine distance is in [0, 2] - 1 - distance is the matching
                    # cosine similarity (1.0 = identical direction), easier for a
                    # caller/model to read as "higher is more relevant".
                    "score": 1.0 - float(row[12]),
                    "content_at": row[13].isoformat(),
                }
            )
        return hits

    async def execute_readonly_query(self, sql: str, limit: int) -> tuple[list[tuple[Any, ...]], list[str]]:
        """Runs an already-validated, single read-only SELECT/WITH statement
        (`search/sql.py`'s Text-to-SQL tool - `sql` has already passed that
        module's string-level checks by the time it reaches here) with two
        more independent layers of defense against it turning out to be a
        write after all:

        1. `SET TRANSACTION READ ONLY` - Postgres itself rejects any write
           statement inside this transaction, regardless of what the
           string-level check upstream missed.
        2. `force_rollback=True` - the transaction is rolled back at the end
           of the block unconditionally, even on success, so nothing this
           query does can persist even in some edge case neither (1) nor the
           upstream check anticipated.

        `limit` is enforced by wrapping the caller's query as a subquery
        (`SELECT * FROM (sql) AS sql_search LIMIT <limit>`) rather than
        trusting whatever `LIMIT` (if any) `sql` itself contains - a result
        set is capped no matter what the caller wrote or forgot to write.
        `limit` is interpolated as a literal `int(limit)` (never passed as a
        `%s` query parameter alongside `sql`) - `sql` is untrusted, arbitrary
        text that legitimately contains literal `%` (e.g. `ILIKE '%김%'`),
        and psycopg's client-side placeholder scanner runs over the *whole*
        query string whenever a `params` tuple is given, so a stray `%` in
        `sql` gets misparsed as a broken placeholder. `int()` makes the
        literal-interpolation safe (whatever `limit` was, only its digits
        ever reach the query)."""
        wrapped = f"SELECT * FROM (\n{sql}\n) AS sql_search LIMIT {int(limit)}"
        async with self._read_conn() as conn:
            # `_tx_lock`은 **공유 커넥션 하나를 쓸 때만** 필요하다. 풀에서 빌린 커넥션은
            # 이 호출이 반납할 때까지 이 호출 전용이라 BEGIN/ROLLBACK이 겹칠 수가 없고,
            # 거기에 전역 락을 걸면 풀을 넣은 이유(질의 여러 개를 겹치는 것)가 그대로
            # 사라진다. `_read_conn`이 공유 커넥션으로 폴백했을 때만 예전처럼 직렬화한다.
            guard: Any = self._tx_lock if conn is self._conn else nullcontext()
            async with guard:
                async with conn.transaction(force_rollback=True):
                    await conn.execute("SET TRANSACTION READ ONLY")
                    async with conn.cursor() as cur:
                        await cur.execute(wrapped)
                        rows = await cur.fetchall()
                        columns = [d.name for d in cur.description] if cur.description else []
        return rows, columns

    async def backfill_document_titles(self, project_id: str | None = None, *, overwrite: bool = False) -> int:
        """Fills `documents.title` for rows written before the column existed
        (`knowledge-forge db backfill-titles`), using the same
        `derive_document_title()` `save_document` applies at write time, so
        a backfilled row and a freshly ingested one can never disagree on
        how a title is chosen. Only `title IS NULL` rows are touched unless
        `overwrite=True` (re-derive everything - for after a change to the
        derivation rules). The first heading is pulled out in SQL (`WITH
        ORDINALITY` keeps document order) so the whole `elements` blob is
        never shipped to Python just to read one string. `updated_at` is
        deliberately left alone: it tracks content revisions, and a title
        fill changes nothing the content-hash/versioning logic cares about.
        Returns how many rows received a non-NULL title."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.metadata->>'title', s.source_type, s.metadata,
                       COALESCE(s.modified_at, s.collected_at),
                       (SELECT e.value->>'text'
                          FROM jsonb_array_elements(d.elements) WITH ORDINALITY AS e(value, ord)
                         WHERE e.value->>'type' = 'heading'
                         ORDER BY e.ord
                         LIMIT 1)
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE (%s::text IS NULL OR d.project_id = %s)
                  AND (%s::boolean OR d.title IS NULL)
                """,
                (project_id, project_id, overwrite),
            )
            rows = await cur.fetchall()

        updates: list[tuple[str, str]] = []
        for document_id, metadata_title, source_type, source_metadata, content_at, first_heading in rows:
            title = derive_document_title(
                metadata_title=metadata_title,
                source_type=source_type,
                source_metadata=source_metadata,
                content_at=content_at,
                first_heading=first_heading,
            )
            if title is not None:
                updates.append((title, document_id))
        if not updates:
            return 0
        async with self.conn.cursor() as cur:
            await cur.executemany("UPDATE documents SET title = %s WHERE document_id = %s", updates)
        return len(updates)

    async def normalize_unicode_text(self, project_id: str | None = None) -> tuple[int, int]:
        """이미 저장된 `documents.title` / `chunks.text` 중 NFC가 아닌 행을 NFC로 고친다
        (`knowledge-forge db normalize-unicode`). 적재 경로에 NFC를 건 뒤 남는 과거 데이터를
        위한 일회성 마이그레이션이고, 재실행해도 0건이 나오는 멱등 연산이다.

        재적재가 아니라 전용 마이그레이션인 이유: 이 행들의 내용은 멀쩡하고 **표현 형태만**
        틀렸다. 재적재는 파싱·추출·임베딩(LLM 호출 포함)을 전부 다시 돌리면서도 결국 같은
        `document_id`/`chunk_id`를 덮어쓸 뿐이라, 표현 형태 하나 고치자고 치르기엔 비싸고
        Gmail처럼 원본을 다시 못 받는 소스도 있다. `db backfill-titles --overwrite`는 제목은
        고칠 수 있지만 `chunks.text`는 못 고치고, 제목 파생 규칙 전체를 재적용하는 넓은 연산이다.

        NFC 판정과 변환을 파이썬이 아니라 서버의 `normalize(text, NFC)`(PG 13+)로 하는 이유는
        쓰기 트랜잭션을 짧게 끝내기 위해서다 - 개발 DB를 다른 프로세스가 동시에 읽는다.
        `updated_at`은 건드리지 않는다: 내용 개정이 아니라 표현 정규화라
        `backfill_document_titles`와 같은 판단이다.

        반환값은 `(고친 documents 행 수, 고친 chunks 행 수)`."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE documents SET title = normalize(title, NFC)
                WHERE title IS NOT NULL AND title <> normalize(title, NFC)
                  AND (%s::text IS NULL OR project_id = %s)
                """,
                (project_id, project_id),
            )
            documents = cur.rowcount
            await cur.execute(
                """
                UPDATE chunks c SET text = normalize(c.text, NFC)
                WHERE c.text <> normalize(c.text, NFC)
                  AND (%s::text IS NULL
                       OR EXISTS (SELECT 1 FROM documents d WHERE d.document_id = c.document_id AND d.project_id = %s))
                """,
                (project_id, project_id),
            )
            chunks = cur.rowcount
        return documents, chunks

    async def search_documents_by_title(
        self,
        query: str,
        *,
        project_id: str | None = None,
        source_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Title/file-name lookup over `documents.title` (search/service.py::
        document_search) for navigational/ambiguous queries - "2025_SH_업무_
        보고서 찾아줘", "SH 보고서" - that need string matching, not an
        embedding. Three match tiers, ranked in this order and each a
        pg_trgm-backed predicate on the one indexed column:

        1. whole-query substring (`title ILIKE '%q%'`) - the user typed (part
           of) the exact name;
        2. every whitespace token a substring (`ILIKE ALL(...)`) - "SH 보고서"
           against "2025_SH_업무_보고서.pdf", where the tokens are present but
           not adjacent;
        3. `word_similarity(q, title) >= _TITLE_SIMILARITY_THRESHOLD` - fuzzy
           fallback for typos/partial words. `word_similarity` rather than
           plain `similarity`: it asks "how much of the *query* is covered by
           the title", so a short query against a long file name still scores
           high (0.7 for "SH 보고서", vs 0.37 with `similarity`), which is the
           question a title search is actually asking.

        `score` is 1.0 for tier 1, `0.5 + sim/2` for tier 2 and the raw
        similarity for tier 3, so it stays in [0, 1] and monotonic with the
        tiers. `since`/`until` filter on `COALESCE(s.modified_at,
        s.collected_at)` - the same real-content timestamp `search_by_embedding`
        uses (its docstring explains why not a pipeline timestamp).

        질의는 NFC로 정규화해서 쓴다(`text_normalize.py`): 사용자가 macOS/Google Drive에서
        복사한 NFD 파일명을 붙여넣어도 NFC로 저장된 제목과 만나야 한다. 적재 쪽 정규화와
        짝이라 한쪽만으로는 절반만 맞는다 - 저장만 고치면 NFD 질의가, 질의만 고치면 이미
        저장된 NFD 행이 남는다."""
        query = to_nfc(query)
        pattern = _like_pattern(query)
        token_patterns = _token_patterns(query)
        async with self._read_conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.title, d.source_id, s.source_type, s.uri, s.modified_at,
                       d.project_id, COALESCE(s.modified_at, s.collected_at) AS content_at,
                       (d.title ILIKE %s) AS is_substring,
                       (d.title ILIKE ALL(%s::text[])) AS all_tokens,
                       word_similarity(%s, d.title) AS sim
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE d.title IS NOT NULL
                  AND (d.title ILIKE %s OR d.title ILIKE ALL(%s::text[]) OR word_similarity(%s, d.title) >= %s)
                  AND (%s::text IS NULL OR d.project_id = %s)
                  AND (%s::text IS NULL OR s.source_type = %s)
                  AND (%s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) >= %s)
                  AND (%s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) < %s)
                ORDER BY is_substring DESC, all_tokens DESC, sim DESC, d.title, d.document_id
                LIMIT %s
                """,
                (
                    pattern,
                    token_patterns,
                    query,
                    pattern,
                    token_patterns,
                    query,
                    _TITLE_SIMILARITY_THRESHOLD,
                    project_id,
                    project_id,
                    source_type,
                    source_type,
                    since,
                    since,
                    until,
                    until,
                    limit,
                ),
            )
            rows = await cur.fetchall()

        hits: list[dict[str, Any]] = []
        for row in rows:
            is_substring, all_tokens, sim = bool(row[8]), bool(row[9]), float(row[10])
            if is_substring:
                score = 1.0
            elif all_tokens:
                score = 0.5 + sim / 2
            else:
                score = sim
            hits.append(
                {
                    "target_kind": "document",
                    "document_id": row[0],
                    "title": row[1],
                    "source_id": row[2],
                    "source_type": row[3],
                    "uri": row[4],
                    "modified_at": row[5].isoformat() if row[5] is not None else None,
                    "project_id": row[6],
                    "content_at": row[7].isoformat(),
                    "score": score,
                }
            )
        return hits

    async def search_chunks_lexical(
        self,
        query: str,
        *,
        project_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Substring/trigram match over `chunks.text` (search/service.py::
        lexical_search) - the exact-identifier leg ("ERR_CONNECTION_RESET",
        a ticket id, a quoted phrase) that `vector_search` structurally can't
        do (an id has no meaning for an embedding to be near). Same tiers as
        `search_documents_by_title` (substring -> all tokens -> fuzzy) with a
        higher fuzzy bar (`_CHUNK_SIMILARITY_THRESHOLD`): chunk bodies are
        long, so a low `word_similarity` matches almost anything.

        Hits use the same key set as `search_by_embedding`'s chunk-grain rows
        (`target_kind='chunk'`, `document_id`, `chunk_id`, `summary`, `score`,
        ...) so `search/rerank.py` and the agent can treat the two
        interchangeably, plus `title`/`source_type`/`uri`/`modified_at` from
        the joined document/source. `summary` is a window of
        `_CHUNK_HIT_SUMMARY_CHARS` around the first match rather than the head
        of the chunk (the match is usually deep in a long chunk, and the head
        would show nothing relevant). The fuzzy tier calls
        `word_similarity()` per row instead of the index-backed `<%` operator
        because that operator reads its threshold from a session GUC; at the
        current table sizes the explicit function call is fast enough, and
        the trigram index still serves the `ILIKE` tiers.

        질의 NFC 정규화 이유는 `search_documents_by_title` 참고 - `_lexical_snippet`의
        일치 지점 탐색도 같은 정규화된 질의를 쓴다."""
        query = to_nfc(query)
        pattern = _like_pattern(query)
        token_patterns = _token_patterns(query)
        async with self._read_conn() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT c.chunk_id, c.document_id, c.source_id, c.hierarchy_level, c.sequence_index, c.text,
                       d.title, d.project_id, s.source_type, s.uri, s.modified_at,
                       COALESCE(s.modified_at, s.collected_at) AS content_at,
                       (c.text ILIKE %s) AS is_substring,
                       word_similarity(%s, c.text) AS sim
                FROM chunks c
                JOIN documents d ON d.document_id = c.document_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE (c.text ILIKE %s OR c.text ILIKE ALL(%s::text[]) OR word_similarity(%s, c.text) >= %s)
                  AND (%s::text IS NULL OR d.project_id = %s)
                  AND (%s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) >= %s)
                  AND (%s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) < %s)
                ORDER BY is_substring DESC, sim DESC, c.document_id, c.sequence_index
                LIMIT %s
                """,
                (
                    pattern,
                    query,
                    pattern,
                    token_patterns,
                    query,
                    _CHUNK_SIMILARITY_THRESHOLD,
                    project_id,
                    project_id,
                    since,
                    since,
                    until,
                    until,
                    limit,
                ),
            )
            rows = await cur.fetchall()

        return [
            {
                "target_kind": "chunk",
                "knowledge_object_id": None,
                "chunk_id": row[0],
                "document_id": row[1],
                "source_id": row[2],
                "type": "chunk",
                "summary": _lexical_snippet(row[5], query, _CHUNK_HIT_SUMMARY_CHARS),
                "topics": [],
                "document_type": None,
                "hierarchy_level": row[3],
                "sequence_index": row[4],
                "title": row[6],
                "project_id": row[7],
                "source_type": row[8],
                "uri": row[9],
                "modified_at": row[10].isoformat() if row[10] is not None else None,
                "content_at": row[11].isoformat(),
                "score": 1.0 if row[12] else float(row[13]),
            }
            for row in rows
        ]

    # --- document_links / version families (document_links.py) ---------

    async def save_document_links(self, links: list[DocumentLink]) -> None:
        rows: list[tuple[Any, ...]] = [
            (
                link.link_id,
                link.from_document_id,
                link.to_document_id,
                link.link_type.value,
                link.confidence,
                Jsonb(link.metadata),
                link.created_at,
            )
            for link in links
        ]
        if not rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO document_links (link_id, from_document_id, to_document_id, link_type,
                                            confidence, metadata, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (link_id) DO UPDATE SET
                    from_document_id = EXCLUDED.from_document_id,
                    to_document_id = EXCLUDED.to_document_id,
                    link_type = EXCLUDED.link_type,
                    confidence = EXCLUDED.confidence,
                    metadata = EXCLUDED.metadata
                """,
                rows,
            )

    async def delete_document_links(self, document_ids: list[str], link_type: str) -> int:
        """Removes every `link_type` edge that *starts* at one of
        `document_ids` - the relink step (document_links.relink_version_family)
        rebuilds a family's PREVIOUS_VERSION chain from scratch, and a stale
        edge from an older run (a version ingested out of order) would
        otherwise survive next to the new one, since stable link_ids only
        dedupe identical edges."""
        if not document_ids:
            return 0
        async with self.conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM document_links WHERE link_type = %s AND from_document_id = ANY(%s)",
                (link_type, document_ids),
            )
            return cur.rowcount

    async def list_document_links(
        self,
        document_id: str,
        *,
        direction: str = "both",
        link_type: str | None = None,
    ) -> list[DocumentLink]:
        """`direction`: "out" (edges starting at `document_id`), "in"
        (edges pointing at it) or "both". Ordered by type then created_at
        so the result is stable across calls."""
        if direction not in ("out", "in", "both"):
            raise ValueError(f"direction must be 'out', 'in' or 'both', not {direction!r}")
        match_out = direction in ("out", "both")
        match_in = direction in ("in", "both")
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT link_id, from_document_id, to_document_id, link_type, confidence, metadata, created_at
                FROM document_links
                WHERE ((%s AND from_document_id = %s) OR (%s AND to_document_id = %s))
                  AND (%s::text IS NULL OR link_type = %s)
                ORDER BY link_type, created_at, link_id
                """,
                (match_out, document_id, match_in, document_id, link_type, link_type),
            )
            rows = await cur.fetchall()
        return [_document_link_from_row(row) for row in rows]

    async def list_version_family(self, family_key: str, project_id: str) -> list[Document]:
        """Every document in `project_id` whose metadata.version_family is
        `family_key`, oldest version first / newest last - ordered in
        Python by `ingest_version.version_sort_key` (초안 < unmarked < vN
        < 수정본 < 최종) rather than a SQL ORDER BY, because that ranking
        is a function of three metadata keys, not a single sortable
        column. Ties (two unmarked `image.png` uploads) fall back to the
        row's created_at, then document_id."""
        from knowledge_forge.ingest_version import version_sort_key

        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.elements, d.metadata, d.created_at, d.updated_at,
                       s.source_id, s.source_type, s.uri, s.project_id
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE d.project_id = %s AND d.metadata->>'version_family' = %s
                """,
                (project_id, family_key),
            )
            rows = await cur.fetchall()
        documents = [
            Document(
                document_id=row[0],
                elements=row[1],
                metadata=row[2],
                created_at=row[3],
                updated_at=row[4],
                source=SourceRef(source_id=row[5], source_type=row[6], uri=row[7], project_id=row[8]),
            )
            for row in rows
        ]
        documents.sort(
            key=lambda d: (
                version_sort_key(d.metadata.version_label, d.metadata.version_number, d.metadata.is_final),
                d.created_at or datetime.min.replace(tzinfo=timezone.utc),
                d.document_id,
            )
        )
        return documents

    async def update_document_metadata(self, document_id: str, patch: dict[str, Any]) -> None:
        """Merges `patch` into documents.metadata (`||`) instead of
        replacing it - the backfill only owns the version_* keys and must
        leave whatever else is already stored (title, page_count, keys
        added by other code paths) untouched."""
        await self.conn.execute(
            "UPDATE documents SET metadata = metadata || %s, updated_at = %s WHERE document_id = %s",
            (Jsonb(patch), datetime.now(timezone.utc), document_id),
        )

    async def find_document_ids_by_source_uris(self, uris: list[str], project_id: str) -> dict[str, str]:
        """uri -> document_id for the sources (in `project_id`) that have a
        parsed Document - how a `sources.metadata.links[].target_uri`
        (context/slack.py, context/gmail.py) is turned into a
        document_links endpoint. A uri whose source was never parsed
        (skipped by the pipeline) is simply absent from the result."""
        if not uris:
            return {}
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.uri, d.document_id
                FROM sources s
                JOIN documents d ON d.source_id = s.source_id
                WHERE s.project_id = %s AND s.uri = ANY(%s)
                """,
                (project_id, uris),
            )
            rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

    async def find_slack_thread_parent_document(
        self, project_id: str, channel: str, thread_ts: str, exclude_source_id: str
    ) -> str | None:
        """The document of the *other* Slack episode in `channel` whose
        message_ts list contains `thread_ts` - i.e. the timeline episode
        that already held the thread's parent message before the replies
        arrived in a later sync (context/slack.py merges parent + replies
        when they arrive together, in which case there is no separate
        parent episode and this returns None)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id
                FROM sources s
                JOIN documents d ON d.source_id = s.source_id
                WHERE s.project_id = %s AND s.source_type = 'slack' AND s.source_id <> %s
                  AND s.metadata->>'channel' = %s AND s.metadata->'message_ts' ? %s
                ORDER BY s.modified_at NULLS LAST, s.source_id
                LIMIT 1
                """,
                (project_id, exclude_source_id, channel, thread_ts),
            )
            row = await cur.fetchone()
        return row[0] if row is not None else None

    async def list_documents_with_sources(
        self, project_id: str | None = None, limit: int = 500, offset: int = 0
    ) -> list[tuple[Document, Source]]:
        """Backfill input (document_links.backfill_document_versions_and_links):
        each Document paired with its full Source (the version parser needs
        `sources.metadata.name`/`subject`, the linker needs `metadata.links`/
        `thread_ts`). `elements` is not loaded - the Document comes back
        with an empty element list, which is all the backfill needs and
        keeps a whole-table pass cheap."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT d.document_id, d.metadata, d.created_at, d.updated_at,
                       s.source_id, s.source_type, s.uri, s.content_hash, s.collected_at,
                       s.modified_at, s.metadata, s.project_id
                FROM documents d
                JOIN sources s ON s.source_id = d.source_id
                WHERE (%s::text IS NULL OR d.project_id = %s)
                ORDER BY d.created_at, d.document_id
                LIMIT %s OFFSET %s
                """,
                (project_id, project_id, limit, offset),
            )
            rows = await cur.fetchall()
        pairs: list[tuple[Document, Source]] = []
        for row in rows:
            source = Source(
                source_id=row[4],
                source_type=row[5],
                uri=row[6],
                content_hash=row[7],
                collected_at=row[8],
                modified_at=row[9],
                metadata=row[10],
                project_id=row[11],
            )
            document = Document(
                document_id=row[0],
                source=SourceRef.from_source(source),
                elements=[],
                metadata=row[1],
                created_at=row[2],
                updated_at=row[3],
            )
            pairs.append((document, source))
        return pairs




# --- lexical search helpers (search_documents_by_title / search_chunks_lexical) ---

# `word_similarity` floor for the fuzzy tier. 0.3 on titles lets "보고서 SH"
# style partial/misspelled names through while keeping unrelated file names
# out (measured on the dev DB's Drive/Gmail titles); chunk bodies need a
# stricter 0.5 because a long text covers a short query's trigrams almost
# by accident.
_TITLE_SIMILARITY_THRESHOLD = 0.3
_CHUNK_SIMILARITY_THRESHOLD = 0.5
# Cap on how many whitespace tokens the all-tokens tier ANDs together - past
# this a query is a sentence, not a name, and belongs to vector_search.
_MAX_QUERY_TOKENS = 8


def _like_pattern(text: str) -> str:
    """`%text%` with LIKE's own metacharacters escaped (backslash is
    Postgres' default LIKE escape). Matters here more than usual: the file
    names this matches against are full of `_` ("2025_SH_업무_보고서"), which
    unescaped is LIKE's single-character wildcard."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _token_patterns(query: str) -> list[str]:
    tokens = query.split()[:_MAX_QUERY_TOKENS]
    return [_like_pattern(token) for token in tokens] or [_like_pattern(query)]


def _lexical_snippet(text: str, query: str, max_chars: int) -> str:
    """`max_chars` of whitespace-normalized `text` around the first
    case-insensitive occurrence of the whole query (else of its first token
    that occurs), or the head of the text when nothing is found (the fuzzy
    tier matched on trigrams, not a literal substring)."""
    normalized = " ".join(text.split())
    lowered = normalized.lower()
    idx = -1
    for needle in [query.lower(), *(token.lower() for token in query.split())]:
        idx = lowered.find(needle)
        if idx != -1:
            break
    if idx == -1 or len(normalized) <= max_chars:
        return normalized[:max_chars]
    start = max(0, idx - max_chars // 3)
    end = min(len(normalized), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(normalized) else ""
    return f"{prefix}{normalized[start:end]}{suffix}"




def _document_link_from_row(row: tuple[Any, ...]) -> DocumentLink:
    return DocumentLink(
        link_id=row[0],
        from_document_id=row[1],
        to_document_id=row[2],
        link_type=row[3],
        confidence=row[4],
        metadata=row[5],
        created_at=row[6],
    )
