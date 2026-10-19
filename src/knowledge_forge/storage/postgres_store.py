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

from knowledge_forge import korean_tokens
from knowledge_forge.config import settings
from knowledge_forge.exceptions import EmbeddingError
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document
from knowledge_forge.models.entity import Entity, EntityMention
from knowledge_forge.models.knowledge_object import (
    DocumentRef,
    Evidence,
    KnowledgeObject,
    validate_content,
)
from knowledge_forge.models.link import DocumentLink
from knowledge_forge.models.relation import TRAVERSAL_RELATION_TYPES, Neighbor, Relation
from knowledge_forge.models.source import Source, SourceRef
from knowledge_forge.storage.document_title import (
    derive_document_title,
    first_heading_text,
    first_substantive_message,
)
from knowledge_forge.storage.title_match import match_titles
from knowledge_forge.text_normalize import to_nfc

if TYPE_CHECKING:
    from knowledge_forge.embedding.base import EmbeddingRecord

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def schema_sql() -> str:
    """schema.sql with the embedding vector width (KF_EMBEDDING_DIMENSIONS)
    filled in - the one place the placeholder is resolved, shared with
    JobStore.init_schema()."""
    return _SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "__EMBEDDING_DIMENSIONS__", str(settings.embedding_dimensions)
    )

# How much of a chunk-grain embedding's `embedded_text` a vector_search hit
# carries as its `summary` (see `search_by_embedding`). A chunk can be up to
# `--max-tokens` (4000 by default) long, and one tool_result holding `limit`
# of those would blow the agent's context budget the same way an unclamped
# `limit` would (agent/tools.py::_MAX_SEARCH_LIMIT) - the agent can
# `read_file` the resource for the full text once it has picked a hit.
_CHUNK_HIT_SUMMARY_CHARS = 500
_BM25_K1 = 1.2
_BM25_B = 0.75

# 읽기 전용 검색 경로(`_pooled_conn`)가 쓰는 커넥션 풀 크기.
#
# 왜 풀이 필요한가: psycopg3의 async 커넥션은 커넥션 하나에 대한 동시 실행을 내부 락으로
# 직렬화한다. 그래서 `search/retrieve.py`의 arm들을 asyncio.gather로 겹쳐도 커넥션이
# 하나면 아무 이득이 없다 - 실측(이 코퍼스, 936청크): lexical_search 5회가 순차 182ms,
# 같은 커넥션에 gather 183ms(= 이득 0), 커넥션 5개로 gather 46ms(약 4배).
#
# max 8인 이유: 동시 질의 몇 개(측정 당시 4)가 각각 arm 안에서 2~3개를 겹치므로
# 상한이 대략 그 곱이다. 그 위로 더 열어도 Postgres가
# 같은 코어에서 같은 일을 하는 것이라 처리량이 늘지 않는다.
#
# min 4인 이유: 풀이 필요할 때만 늘어나면 첫 버스트가 커넥션 하나를 놓고 줄을 선다
# (실측: 같은 gather 5회가 min=1이면 93ms, min=4면 57ms). 풀 자체가 **첫 검색 때** 열리므로
# (아래 `_pooled_conn` 참고) 검색을 한 번도 안 하는 소비자(적재 파이프라인, 워커, 스케줄러)는
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
        # 읽기 전용 검색용 풀. `connect()`가 아니라 첫 검색 때 만들어진다(`_pooled_conn`).
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
    async def _pooled_conn(self) -> AsyncIterator[psycopg.AsyncConnection]:
        """커넥션 하나를 이 호출 전용으로 빌려준다 - 풀에서 하나, 풀을 못 열면
        기존의 공유 커넥션.

        검색 메서드(`search_by_embedding` / `search_documents_by_title` /
        `search_chunks_lexical` / `execute_readonly_query`)와, 여러 문장을 한 트랜잭션으로
        묶어야 하는 `save_chunks(document_id=...)`가 이걸 쓴다. 나머지 쓰기는 단일 문장
        autocommit이라 공유 커넥션 `self.conn`에서 돈다 - psycopg가 문장 단위로 직렬화하므로
        문서 여러 개가 동시에 써도 안전하다. 반대로 공유 커넥션에서 트랜잭션을 열면 동시에 도는
        다른 코루틴의 문장이 그 트랜잭션에 섞이므로, 트랜잭션은 반드시 여기서 빌린 커넥션에서
        연다(풀 폴백 시에는 그 보장이 없다 - 폴백은 풀을 열 수 없는 비정상 상황용이다).

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
        await self.conn.execute(schema_sql())
        await self._ensure_embedding_dimensions()
        # The schema above is what actually creates the `vector` extension
        # on a brand new database - `connect()`'s registration attempt was
        # a no-op if this is the first time this database has ever seen it,
        # so retry it now that the type genuinely exists.
        await self._register_vector_types()
        await self._configure_ann_search()

    async def _stored_embedding_dimensions(self) -> int | None:
        """Declared width of knowledge_object_embeddings.embedding (the
        pgvector typmod is the dimension itself), None if no such table."""
        cur = await self.conn.execute(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = to_regclass('knowledge_object_embeddings') AND attname = 'embedding'"
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def _ensure_embedding_dimensions(self) -> None:
        stored = await self._stored_embedding_dimensions()
        if stored is not None and stored != settings.embedding_dimensions:
            raise EmbeddingError(
                f"DB의 임베딩 차원은 {stored}인데 KF_EMBEDDING_DIMENSIONS={settings.embedding_dimensions}입니다. "
                "기존 임베딩을 지우고 컬럼과 HNSW 인덱스를 새 차원으로 바꾸려면 "
                "`knowledge-forge db migrate-embedding-dimensions`를 실행한 뒤 `knowledge-forge embed`로 다시 채우세요."
            )

    async def migrate_embedding_dimensions(self) -> bool:
        """Converts knowledge_object_embeddings to KF_EMBEDDING_DIMENSIONS. Vectors of
        another width can't be cast, so ALL existing embeddings are deleted (they are
        derivable - re-run `knowledge-forge embed`); the HNSW index is rebuilt by the
        column type change. Returns False when the width already matches."""
        await self.conn.execute(schema_sql())  # creates the table if absent
        stored = await self._stored_embedding_dimensions()
        if stored == settings.embedding_dimensions:
            return False
        async with self.conn.transaction():
            await self.conn.execute("DELETE FROM knowledge_object_embeddings")
            await self.conn.execute("DROP INDEX IF EXISTS idx_koe_embedding_hnsw")
            await self.conn.execute(
                f"ALTER TABLE knowledge_object_embeddings ALTER COLUMN embedding "
                f"TYPE vector({settings.embedding_dimensions})"
            )
            await self.conn.execute(
                "CREATE INDEX idx_koe_embedding_hnsw ON knowledge_object_embeddings "
                "USING hnsw (embedding vector_cosine_ops)"
            )
        return True

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

    async def get_source_content_hash(self, source_id: str) -> str | None:
        """`source_id`'s `content_hash` - but only if a `documents` row for it
        already exists, so a single read answers `run_pipeline`'s "is this
        item unchanged and already fully processed" check (see
        `PipelineStore.get_source_content_hash`): a source that was collected
        but never produced a document (parse failure, or a fresh source)
        returns None here just like a source that has never been seen, and
        both mean "do not skip - process it"."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.content_hash
                FROM sources s
                WHERE s.source_id = %s
                  AND EXISTS (SELECT 1 FROM documents d WHERE d.source_id = s.source_id)
                """,
                (source_id,),
            )
            row = await cur.fetchone()
        return row[0] if row else None

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

    async def save_chunks(self, chunks: list[Chunk], *, document_id: str | None = None) -> None:
        """청크 본문은 NFC로 정규화된 채로 들어온다(`chunking/adaptive.py::AdaptiveChunker.chunk`가
        `text_normalize.py::to_nfc`를 적용해서 만든다) - 여기서 다시 정규화하지 않는다. 이걸
        DB 쓰기 시점이 아니라 청커 출력 시점에 거는 이유: `chunks.text`는
        `search_chunks_lexical`의 ILIKE/trigram 대상이면서(macOS·Drive 유래 NFD 파일명이
        본문에 인용돼 들어오면 NFC 질의로는 구조적으로 못 맞는다) **Knowledge Extraction
        프롬프트와 `record_chunk_embeddings`에도 그대로 들어가는 메모리 상의 문자열**이라,
        DB 행에만 정규화를 걸면 그 두 소비자는 여전히 정규화 안 된 텍스트를 본다. 파서 출력
        원본인 `documents.elements`는 그대로 두고 청커 출력에서만 정규화한다 - 청크는
        `element_ids`로 엘리먼트를 참조하지 문자열로 맞대보지 않으므로 둘이 달라도 깨지는
        곳이 없다. `token_count`는 재계산하지 않는다: NFC 합성은 코드포인트 수만 줄일 뿐이고,
        토큰 수는 어차피 청킹 시점의 근사치다.

        `document_id`를 주면 `chunks`가 그 문서의 **청크 집합 전체**라는 뜻이다: 그 문서의 기존
        청크 중 `chunks`에 없는 것과 그 청크-grain 임베딩 행을 upsert와 같은 트랜잭션에서 지운다
        (`chunks`가 비면 문서의 청크가 전부 지워진다). 파서·청커가 바뀌어 같은 `document_id`가 더
        적은 청크로 다시 나오면(작은 형제 병합, VLM 실패로 사라진 표 등) 예전 청크가 검색에 계속
        걸리던 구멍을 막는다(wiki/reingest-replay.md "재적재 시 문서의 청크 집합 교체").
        `knowledge_objects.evidence`/`relations.evidence`의 chunk_id는 FK가 아닌 JSON 참조라
        지워진 청크를 가리킨 채 남을 수 있다 - 그걸 역참조하는 읽기 경로는 없다.
        트랜잭션은 풀에서 빌린 전용 커넥션에서 연다(`_pooled_conn`) - 공유 커넥션에서 열면
        동시에 도는 다른 문서의 autocommit 문장이 그 트랜잭션에 섞인다(pipeline.py 문서 간 동시성)."""
        now = datetime.now(timezone.utc)
        rows: list[tuple[Any, ...]] = [
            (
                chunk.chunk_id,
                chunk.document_id,
                chunk.source_id,
                chunk.hierarchy_level.value,
                chunk.sequence_index,
                chunk.parent_chunk_id,
                chunk.text,
                chunk.token_count,
                Jsonb(chunk.element_ids),
                chunk.page_range[0] if chunk.page_range else None,
                chunk.page_range[1] if chunk.page_range else None,
                now,
                now,
            )
            for chunk in chunks
        ]
        if document_id is None and not rows:
            return
        # BM25 색인(`KF_SEARCH_BM25`)은 트랜잭션 밖에서 토큰화한다 - 형태소 분석은 CPU 작업이라 커넥션을 쥔 채 하지 않는다.
        terms = await self._chunk_terms([(chunk.chunk_id, chunk.text) for chunk in chunks]) if settings.search_bm25 else []
        async with self._pooled_conn() as conn, conn.transaction(), conn.cursor() as cur:
            if document_id is not None:
                stale = "SELECT chunk_id FROM chunks WHERE document_id = %(doc)s AND NOT chunk_id = ANY(%(keep)s)"
                params = {"doc": document_id, "keep": [chunk.chunk_id for chunk in chunks]}
                # 임베딩 FK(chunk_id -> chunks)가 있어 임베딩 행을 먼저 지운다.
                await cur.execute(f"DELETE FROM knowledge_object_embeddings WHERE chunk_id IN ({stale})", params)
                await cur.execute(f"DELETE FROM chunks WHERE chunk_id IN ({stale})", params)
            if not rows:
                return
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
                    -- 본문이 바뀌면 BM25 색인은 낡았다 - 미색인(NULL)으로 돌려 검색에서 빼고, 아래/백필이 다시 채운다.
                    lex_len = CASE WHEN chunks.text = EXCLUDED.text THEN chunks.lex_len ELSE NULL END,
                    text = EXCLUDED.text,
                    token_count = EXCLUDED.token_count,
                    element_ids = EXCLUDED.element_ids,
                    page_start = EXCLUDED.page_start,
                    page_end = EXCLUDED.page_end,
                    updated_at = EXCLUDED.updated_at
                """,
                rows,
            )
            await self._write_chunk_terms(cur, terms)

    @staticmethod
    async def _chunk_terms(chunks: list[tuple[str, str]]) -> list[tuple[str, dict[str, int]]]:
        """(chunk_id, text) -> (chunk_id, {term: tf}) - Kiwi 분석을 스레드에서 돌린다."""

        def analyze() -> list[tuple[str, dict[str, int]]]:
            out = []
            for chunk_id, text in chunks:
                counts: dict[str, int] = {}
                for term in korean_tokens.tokenize(text):
                    counts[term] = counts.get(term, 0) + 1
                out.append((chunk_id, counts))
            return out

        return await asyncio.to_thread(analyze)

    @staticmethod
    async def _write_chunk_terms(cur: psycopg.AsyncCursor, terms: list[tuple[str, dict[str, int]]]) -> None:
        """`chunk_terms`를 청크별로 갈아끼우고 `chunks.lex_len`(term 수)을 채운다. 호출자의 트랜잭션 안에서 돈다."""
        if not terms:
            return
        await cur.execute("DELETE FROM chunk_terms WHERE chunk_id = ANY(%s)", ([chunk_id for chunk_id, _ in terms],))
        await cur.executemany(
            "INSERT INTO chunk_terms (chunk_id, term, tf) VALUES (%s, %s, %s)",
            [(chunk_id, term, tf) for chunk_id, counts in terms for term, tf in counts.items()],
        )
        await cur.executemany(
            "UPDATE chunks SET lex_len = %s WHERE chunk_id = %s",
            [(sum(counts.values()), chunk_id) for chunk_id, counts in terms],
        )

    async def backfill_chunk_terms(self, project_id: str | None = None, *, overwrite: bool = False, batch: int = 500) -> int:
        """`knowledge-forge db backfill-bm25` - `lex_len IS NULL`(미색인)인 청크에 BM25 색인을 채운다. `overwrite`면
        전부 다시(토크나이저를 바꾼 뒤). 배치마다 트랜잭션을 끊어 오래 걸려도 락을 길게 쥐지 않는다."""
        done = 0
        last = ""
        while True:
            async with self.conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT c.chunk_id, c.text FROM chunks c JOIN documents d ON d.document_id = c.document_id
                    WHERE c.chunk_id > %s AND (%s::text IS NULL OR d.project_id = %s) AND (%s::boolean OR c.lex_len IS NULL)
                    ORDER BY c.chunk_id LIMIT %s
                    """,
                    (last, project_id, project_id, overwrite, batch),
                )
                rows = await cur.fetchall()
            if not rows:
                return done
            terms = await self._chunk_terms([(chunk_id, text) for chunk_id, text in rows])
            async with self._pooled_conn() as conn, conn.transaction(), conn.cursor() as cur:
                await self._write_chunk_terms(cur, terms)
            done += len(rows)
            last = rows[-1][0]

    async def search_chunks_bm25(
        self,
        query: str,
        *,
        project_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """청크 본문 BM25 검색(`search/service.py::lexical_search`가 `KF_SEARCH_BM25`일 때 부른다). 질의를 색인과 같은
        Kiwi 토큰으로 쪼개 `chunk_terms`에서 posting을 읽고 Okapi BM25(k1=1.2, b=0.75)를 SQL에서 합산한다.
        N/평균 길이는 색인된 청크 전체, df는 질의 term별 posting 수다 - `project_id`/시간 조건은 점수에 안
        들어가고 후보만 거른다(프로젝트마다 IDF가 달라지지 않게). 히트 모양은 `search_chunks_lexical`과 같고
        `score`는 BM25 원점수(상한이 없다 - 소스 안 순위용이지 소스 간 비교용이 아니다)."""
        terms = sorted(set(await asyncio.to_thread(korean_tokens.tokenize, query)))
        if not terms:
            return []
        async with self._read_transaction() as cur:
            await cur.execute(
                """
                WITH stats AS (SELECT count(*)::float AS n, avg(lex_len)::float AS avgdl FROM chunks WHERE lex_len IS NOT NULL),
                df AS (SELECT term, count(*)::float AS df FROM chunk_terms WHERE term = ANY(%(terms)s) GROUP BY term),
                scored AS (
                    SELECT ct.chunk_id,
                           sum(ln(1 + (stats.n - df.df + 0.5) / (df.df + 0.5))
                               * ct.tf * (%(k1)s + 1) / (ct.tf + %(k1)s * (1 - %(b)s + %(b)s * c.lex_len / stats.avgdl))) AS bm25
                    FROM chunk_terms ct
                    JOIN df ON df.term = ct.term
                    JOIN chunks c ON c.chunk_id = ct.chunk_id AND c.lex_len IS NOT NULL
                    JOIN documents d ON d.document_id = c.document_id
                    JOIN sources s ON s.source_id = d.source_id
                    CROSS JOIN stats
                    WHERE ct.term = ANY(%(terms)s)
                      AND (%(project_id)s::text IS NULL OR d.project_id = %(project_id)s)
                      AND (%(since)s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) >= %(since)s)
                      AND (%(until)s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) < %(until)s)
                    GROUP BY ct.chunk_id
                )
                SELECT c.chunk_id, c.document_id, c.source_id, c.hierarchy_level, c.sequence_index, c.text,
                       d.title, d.project_id, s.source_type, s.uri, s.modified_at,
                       COALESCE(s.modified_at, s.collected_at) AS content_at, scored.bm25
                FROM scored
                JOIN chunks c ON c.chunk_id = scored.chunk_id
                JOIN documents d ON d.document_id = c.document_id
                JOIN sources s ON s.source_id = d.source_id
                ORDER BY scored.bm25 DESC, c.document_id, c.sequence_index
                LIMIT %(limit)s
                """,
                {
                    "terms": terms,
                    "k1": _BM25_K1,
                    "b": _BM25_B,
                    "project_id": project_id,
                    "since": since,
                    "until": until,
                    "limit": limit,
                },
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
                "summary": _lexical_snippet(row[5], " ".join(terms), _CHUNK_HIT_SUMMARY_CHARS),
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
                "score": float(row[12]),
            }
            for row in rows
        ]

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
        """`content` is checked against the object type's typed model
        (models/knowledge_object.py::validate_content) before anything is
        written - a malformed decision/action_item/event/entity raises
        pydantic.ValidationError instead of landing as an arbitrary dict."""
        rows: list[tuple[Any, ...]] = [
            (
                ko.knowledge_object_id,
                ko.type.value,
                ko.source.source_id,
                ko.document.document_id,
                Jsonb([e.model_dump(mode="json") for e in ko.evidence]),
                ko.summary,
                Jsonb(validate_content(ko.type, ko.content)),
                Jsonb(ko.topics),
                ko.document_type,
                ko.extracted_at,
                ko.model_used,
                ko.confidence,
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
                                                extracted_at, model_used, confidence, version,
                                                pipeline_version, source_hash, supersedes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    confidence = EXCLUDED.confidence,
                    version = EXCLUDED.version,
                    pipeline_version = EXCLUDED.pipeline_version,
                    source_hash = EXCLUDED.source_hash,
                    supersedes = EXCLUDED.supersedes
                """,
                rows,
            )

    async def list_knowledge_objects_by_source(self, source_id: str) -> list[KnowledgeObject]:
        """The current "head" KnowledgeObjects for this `source_id` - the
        candidate pool extraction/versioning.py matches a re-extraction's
        new objects against to auto-fill `supersedes`.

        Restricted to objects nothing else `supersedes` (the `NOT EXISTS`
        below), rather than every KnowledgeObject ever extracted from this
        source across every past revision. Two ways to define "the latest
        prior revision" were on the table: (a) objects belonging to the
        most recently *created* `documents` row for this source, or (b)
        objects no other object supersedes. (a) is wrong here specifically
        because of *when* this method is called: `run_pipeline` already
        wrote the current run's `documents` row (`record_document`) before
        it calls this to fetch *prior* objects, so "most recent document"
        would resolve to the run's own new, not-yet-extracted document and
        return nothing to match against - silently breaking every
        supersedes link. (b) has no such race (it only looks at
        `knowledge_objects` rows, which the current run hasn't written yet
        at this point) and is also more robust to a document that changes
        only partially between revisions: if revision N's re-extraction
        doesn't happen to touch some object O from revision N-1 (O's claim
        just didn't come up again), O has no successor, stays a "head", and
        remains a match candidate for revision N+1 - restricting to "N-1's
        document" would have silently dropped O from the pool the moment
        N shipped, even though O was never actually superseded. This also
        directly bounds the O(new x prior) similarity matching in
        extraction/versioning.py::resolve_supersedes(): once a claim has
        been re-extracted at all, its old versions drop out of the "head"
        set for good, so the candidate pool tracks the number of still-live
        claims rather than growing with the number of past revisions.

        Joins `sources` for `source_type`/`uri` since `knowledge_objects`
        only stores the id (see models/knowledge_object.py's DocumentRef/
        SourceRef note on why those stay lightweight refs)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_KO_COLUMNS}
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE ko.source_id = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_objects newer
                      WHERE newer.source_id = ko.source_id AND newer.supersedes = ko.knowledge_object_id
                  )
                """,
                (source_id,),
            )
            rows = await cur.fetchall()

        return [_ko_from_row(row) for row in rows]

    async def list_knowledge_objects(
        self,
        source_id: str | None = None,
        type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        project_id: str | None = None,
    ) -> list[KnowledgeObject]:
        """General-purpose paginated listing for the read API (wiki/api.md),
        as opposed to `list_knowledge_objects_by_source` above which exists
        specifically for supersedes-matching. `type`/`source_id`/`project_id`
        are optional filters (%s::text IS NULL trick short-circuits an unset
        filter without a second SQL string)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_KO_COLUMNS}
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE (%(source)s::text IS NULL OR ko.source_id = %(source)s)
                  AND (%(type)s::text IS NULL OR ko.type = %(type)s)
                  AND (%(project)s::text IS NULL OR s.project_id = %(project)s)
                ORDER BY ko.extracted_at DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {"source": source_id, "type": type, "project": project_id, "limit": limit, "offset": offset},
            )
            rows = await cur.fetchall()

        return [_ko_from_row(row) for row in rows]

    async def get_knowledge_object(
        self, knowledge_object_id: str, *, project_id: str | None = None
    ) -> KnowledgeObject | None:
        """None also when the object belongs to a project other than
        `project_id` - a scoped caller can't tell "elsewhere" from "absent"."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_KO_COLUMNS}
                FROM knowledge_objects ko
                JOIN sources s ON s.source_id = ko.source_id
                WHERE ko.knowledge_object_id = %s
                  AND (%s::text IS NULL OR s.project_id = %s)
                """,
                (knowledge_object_id, project_id, project_id),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return _ko_from_row(row)

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

    async def save_entities(self, entities: list[Entity], mentions: list[EntityMention]) -> None:
        """Upserts resolved entities (entity_resolution.py) and their
        mentions. On conflict an entity keeps its first `display_name` and
        unions `aliases`, so resolving the same person in a later document
        only adds the new surface forms. Mentions' objects must already be
        saved (FK to knowledge_objects)."""
        now = datetime.now(timezone.utc)
        entity_rows = [
            (e.entity_id, e.project_id, e.entity_type, e.name, e.display_name, Jsonb(e.aliases), now, now)
            for e in entities
        ]
        project_by_entity = {e.entity_id: e.project_id for e in entities}
        mention_rows = [
            (
                m.entity_id,
                m.knowledge_object_id,
                m.role.value,
                m.surface_form,
                Jsonb(m.evidence),
                project_by_entity[m.entity_id],
                now,
            )
            for m in mentions
        ]
        if not entity_rows:
            return
        async with self.conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO entities (entity_id, project_id, entity_type, name, display_name, aliases,
                                      created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_id) DO UPDATE SET
                    aliases = (
                        SELECT jsonb_agg(alias ORDER BY first_seen)
                        FROM (
                            SELECT alias, min(ord) AS first_seen
                            FROM jsonb_array_elements_text(entities.aliases || EXCLUDED.aliases)
                                 WITH ORDINALITY AS a(alias, ord)
                            GROUP BY alias
                        ) merged
                    ),
                    updated_at = EXCLUDED.updated_at
                """,
                entity_rows,
            )
            if mention_rows:
                await cur.executemany(
                    """
                    INSERT INTO entity_mentions (entity_id, knowledge_object_id, role, surface_form,
                                                 evidence, project_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (entity_id, knowledge_object_id, role) DO UPDATE SET
                        surface_form = EXCLUDED.surface_form,
                        evidence = EXCLUDED.evidence
                    """,
                    mention_rows,
                )

    async def find_entities(
        self, project_id: str | None, name: str, *, entity_type: str | None = None, limit: int = 10
    ) -> list[Entity]:
        """Exact lookup: `name` is normalized the same way resolution does
        (so "정민찬 팀장님" finds 정민찬), and an exact alias match also
        counts. `project_id=None` searches every project - each Entity still
        carries its own, since an entity never spans two."""
        from knowledge_forge.entity_resolution import clean_surface, normalize_entity_name, normalize_entity_type

        types = [normalize_entity_type(entity_type)] if entity_type else ["person", "organization", ""]
        keys = sorted({k for t in types if (k := normalize_entity_name(name, t))})
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT entity_id, project_id, entity_type, name, display_name, aliases
                FROM entities
                WHERE (%(project)s::text IS NULL OR project_id = %(project)s)
                  AND (%(type)s::text IS NULL OR entity_type = %(type)s)
                  AND (name = ANY(%(keys)s) OR aliases ? %(surface)s)
                ORDER BY entity_type, name
                LIMIT %(limit)s
                """,
                {
                    "project": project_id,
                    "type": normalize_entity_type(entity_type) if entity_type else None,
                    "keys": keys,
                    "surface": clean_surface(name),
                    "limit": limit,
                },
            )
            rows = await cur.fetchall()
        return [
            Entity(entity_id=r[0], project_id=r[1], entity_type=r[2], name=r[3], display_name=r[4], aliases=r[5])
            for r in rows
        ]

    async def list_entity_mentions(self, entity_id: str, *, limit: int = 100) -> list[EntityMention]:
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT entity_id, knowledge_object_id, role, surface_form, evidence
                FROM entity_mentions
                WHERE entity_id = %s
                ORDER BY created_at, knowledge_object_id, role
                LIMIT %s
                """,
                (entity_id, limit),
            )
            rows = await cur.fetchall()
        return [
            EntityMention(entity_id=r[0], knowledge_object_id=r[1], role=r[2], surface_form=r[3], evidence=r[4])
            for r in rows
        ]

    async def list_entity_objects(
        self, entity_id: str, *, roles: list[str] | None = None, limit: int = 50
    ) -> list[tuple[str, KnowledgeObject]]:
        """Entity -> KnowledgeObject expansion: `(role, object)` for every
        current (not superseded) object that mentions `entity_id`, newest
        first. Scoping is the entity's own - a mention never crosses
        projects (entity_resolution.py)."""
        async with self.conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT m.role, {_KO_COLUMNS}
                FROM entity_mentions m
                JOIN knowledge_objects ko ON ko.knowledge_object_id = m.knowledge_object_id
                JOIN sources s ON s.source_id = ko.source_id
                WHERE m.entity_id = %(entity)s
                  AND (%(roles)s::text[] IS NULL OR m.role = ANY(%(roles)s))
                  AND {_HEAD_KO}
                ORDER BY ko.extracted_at DESC, ko.knowledge_object_id, m.role
                LIMIT %(limit)s
                """,
                {"entity": entity_id, "roles": roles, "limit": limit},
            )
            rows = await cur.fetchall()
        return [(row[0], _ko_from_row(row[1:])) for row in rows]

    async def neighbors(
        self,
        object_id: str,
        *,
        project_id: str | None = None,
        depth: int = 1,
        relation_types: list[str] | None = None,
        limit: int = 50,
    ) -> tuple[KnowledgeObject, list[Neighbor]] | None:
        """Breadth-first walk over `relations` in both directions from
        `object_id`, at most `depth` hops and `limit` objects, one query per
        hop. None if the start object doesn't exist or is outside
        `project_id`. Two invariants regardless of what the caller passes:
        the walk never leaves the start object's project, and superseded
        objects (an older version some newer object `supersedes`) are never
        returned - their successor is the current claim. Each object comes
        back once, with the edge that first reached it (shortest path);
        within a hop, higher-confidence edges win the `limit`.
        `relation_types` defaults to TRAVERSAL_RELATION_TYPES."""
        start = await self.get_knowledge_object(object_id, project_id=project_id)
        if start is None:
            return None
        types = relation_types if relation_types is not None else [t.value for t in TRAVERSAL_RELATION_TYPES]
        seen = {object_id}
        frontier = [object_id]
        found: list[Neighbor] = []
        for hop in range(1, depth + 1):
            if not frontier or len(found) >= limit:
                break
            async with self.conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT r.relation_id, r.type, r.subject_id, r.object_id, r.label, r.confidence,
                           r.evidence, r.created_at, {_KO_COLUMNS}
                    FROM relations r
                    JOIN knowledge_objects ko ON ko.knowledge_object_id =
                         CASE WHEN r.subject_id = ANY(%(frontier)s) THEN r.object_id ELSE r.subject_id END
                    JOIN sources s ON s.source_id = ko.source_id
                    WHERE (r.subject_id = ANY(%(frontier)s) OR r.object_id = ANY(%(frontier)s))
                      AND r.type = ANY(%(types)s)
                      AND s.project_id = %(project)s
                      AND NOT ko.knowledge_object_id = ANY(%(seen)s)
                      AND {_HEAD_KO}
                    ORDER BY r.confidence DESC NULLS LAST, r.relation_id
                    LIMIT %(limit)s
                    """,
                    {
                        "frontier": frontier,
                        "types": types,
                        "project": start.source.project_id,
                        "seen": list(seen),
                        "limit": limit - len(found),
                    },
                )
                rows = await cur.fetchall()
            frontier = []
            for row in rows:
                ko = _ko_from_row(row[8:])
                if ko.knowledge_object_id in seen:
                    continue
                seen.add(ko.knowledge_object_id)
                frontier.append(ko.knowledge_object_id)
                relation = Relation(
                    relation_id=row[0], type=row[1], subject_id=row[2], object_id=row[3],
                    label=row[4], confidence=row[5], evidence=row[6], created_at=row[7],
                )
                found.append(Neighbor(knowledge_object=ko, depth=hop, relation=relation))
        return start, found

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
                f"""
                SELECT {_KO_COLUMNS}
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

        return [_ko_from_row(row) for row in rows]

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
        - `dimensions`: 실제로 저장된 벡터 차원 집합. 스키마 차원(KF_EMBEDDING_DIMENSIONS)과 다른
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
        document_ids: list[str] | None = None,
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
        the filter actually matched against.

        `document_ids` restricts the search to those documents' rows - used
        to pick a ranked document's best grain units (search/retrieve.py's
        grain expansion). With it the planner uses `idx_koe_document_id` and
        sorts the few matching rows exactly (measured 1 ms), not the HNSW scan."""
        vector = Vector(embedding)
        async with self._pooled_conn() as conn, conn.cursor() as cur:
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
                  AND (%s::text[] IS NULL OR e.document_id = ANY(%s))
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
                    document_ids,
                    document_ids,
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
        async with self._read_transaction() as cur:
            await cur.execute(wrapped)
            rows = await cur.fetchall()
            columns = [d.name for d in cur.description] if cur.description else []
        return rows, columns

    @asynccontextmanager
    async def _read_transaction(self, *settings: str) -> AsyncIterator[psycopg.AsyncCursor]:
        """`_pooled_conn` 위에 `READ ONLY` + 무조건 롤백 트랜잭션을 열고 커서를 준다.
        `settings`는 그 트랜잭션에만 걸리는 `SET LOCAL` 문(상수 문자열만 - 파라미터 없이
        한 번에 보낸다)이다.

        `_tx_lock`은 **공유 커넥션 하나를 쓸 때만** 필요하다. 풀에서 빌린 커넥션은
        이 호출이 반납할 때까지 이 호출 전용이라 BEGIN/ROLLBACK이 겹칠 수가 없고,
        거기에 전역 락을 걸면 풀을 넣은 이유(질의 여러 개를 겹치는 것)가 그대로
        사라진다. `_pooled_conn`이 공유 커넥션으로 폴백했을 때만 예전처럼 직렬화한다."""
        async with self._pooled_conn() as conn:
            guard: Any = self._tx_lock if conn is self._conn else nullcontext()
            async with guard, conn.transaction(force_rollback=True), conn.cursor() as cur:
                await cur.execute("; ".join(["SET TRANSACTION READ ONLY", *settings]))
                yield cur

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

    async def backfill_slack_display_names(
        self,
        directory: Any,
        project_id: str | None = None,
        refresh_first_message: bool = False,
    ) -> dict[str, int]:
        """이미 적재된 슬랙 `sources.metadata`에 사람이 읽는 이름을 채우고, 그 결과로
        `documents.title`을 다시 유도한다(`knowledge-forge db backfill-slack-names`).

        왜 필요한가: 개발 DB의 슬랙 문서 197건은 전부 제목이 `#C0BGQEQG1L6 2026-09-01`,
        참여자가 `["U0BBCPVEHF0"]`였다. v0 골든셋의 정답 문서 90건 중 48건이 슬랙인데,
        제목 조회(`search_documents_by_title`)로 닿는 슬랙 정답은 **66건 중 0건**이었다
        (오프라인 실측). 재적재가 아니라 전용 백필인 이유는
        `normalize_unicode_text`와 같다 - 내용은 멀쩡하고 **표현만** 틀렸으며, 재적재는
        파싱/추출/임베딩을 전부 다시 돌리면서 결국 같은 id를 덮어쓸 뿐이다. 게다가 슬랙
        재적재는 `chunks`/`knowledge_object_embeddings`까지 다시 쓰므로, 같은 DB를 공유하는
        다른 작업의 인덱스 정합을 깬다.

        `directory`는 `collectors/saas/slack.py::SlackDirectory` 모양의 무엇이든 된다
        (`channel_name(id)` / `user_names(ids)` / `resolve_mentions(text)`). 스토리지 계층이
        Slack SDK를 import하지 않게 하려고 프로토콜만 받는다 - 호출부(cli.py)가 만들어 넘긴다.

        **이 메서드는 `sources.metadata`와 `documents.title` 두 컬럼만 쓴다.**
        `chunks`/`knowledge_object_embeddings`/`knowledge_objects`는 손대지 않는다(같은 DB에서
        도는 다른 평가가 깨지지 않아야 한다). `sources.metadata`의 원시 기록
        (`channel`/`participants`/`message_ts`)도 지우지 않고 키만 덧붙인다 - `channel`은
        `find_slack_thread_*`/`document_links.py`의 조인 키다.

        멱등하다: 이미 같은 값이 들어 있으면 UPDATE를 아예 보내지 않으므로, 두 번째 실행은
        전부 0건이 나온다.

        `first_message`는 수집기 경로(`context/slack.py::_merge`)가 채우지만, 이미 적재된
        행에는 없다. 그래서 여기서는 `documents.elements`의 첫 텍스트 블록에서 되살린다 -
        그게 transcript의 첫 메시지고, 화자 접두어(`U0BD…: `)는
        `document_title.slack_excerpt`가 떼어 준다.

        반환값은 통계 dict."""
        stats = {
            "sources_scanned": 0,
            "channels_resolved": 0,
            "channels_unresolved": 0,
            "participants_resolved": 0,
            "first_messages_filled": 0,
            "sources_updated": 0,
            "titles_updated": 0,
        }
        async with self.conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.source_id, s.metadata, d.document_id, d.title, d.metadata->>'title',
                       COALESCE(s.modified_at, s.collected_at),
                       -- 첫 텍스트 블록 하나가 아니라 앞쪽 몇 개를 들고 온다.
                       -- 슬랙 에피소드는 "…님이 채널에 참여함" 가입 알림으로 시작하는
                       -- 일이 흔한데(197건 중 52건), 발췌로는 쓸모가 없어 Python 쪽
                       -- `first_substantive_message`가 건너뛰어야 한다. 술어를 SQL에
                       -- 두지 않는 이유는 수집기 경로와 판정이 갈리면 안 되기 때문이다.
                       (SELECT array_agg(e.value->>'text' ORDER BY e.ord)
                          FROM (SELECT value, ord
                                  FROM jsonb_array_elements(d.elements) WITH ORDINALITY AS x(value, ord)
                                 WHERE x.value->>'type' = 'text'
                                 ORDER BY x.ord
                                 LIMIT %s) AS e(value, ord)),
                       (SELECT e.value->>'text'
                          FROM jsonb_array_elements(d.elements) WITH ORDINALITY AS e(value, ord)
                         WHERE e.value->>'type' = 'heading'
                         ORDER BY e.ord
                         LIMIT 1)
                FROM sources s
                JOIN documents d ON d.source_id = s.source_id
                WHERE s.source_type = 'slack'
                  AND (%s::text IS NULL OR d.project_id = %s)
                ORDER BY s.source_id
                """,
                (_SLACK_EXCERPT_CANDIDATES, project_id, project_id),
            )
            rows = await cur.fetchall()

        source_updates: dict[str, dict[str, Any]] = {}
        title_updates: list[tuple[str, str]] = []
        unresolved_channels: set[str] = set()
        for source_id, metadata, document_id, current_title, metadata_title, content_at, first_texts, first_heading in rows:
            first_text = first_substantive_message(first_texts or ())
            stats["sources_scanned"] += 1
            metadata = dict(metadata or {})
            enriched = source_updates.get(source_id, metadata)
            changed = False

            channel = enriched.get("channel")
            if channel and not enriched.get("channel_name"):
                name = directory.channel_name(str(channel))
                if name:
                    enriched["channel_name"] = name
                    stats["channels_resolved"] += 1
                    changed = True
                else:
                    unresolved_channels.add(str(channel))

            participants = enriched.get("participants")
            if isinstance(participants, list) and participants and not enriched.get("participant_names"):
                names = directory.user_names(participants)
                # 하나도 못 풀었으면(전부 원시 ID 그대로) 키를 만들지 않는다 - 이름이 있는
                # 척하는 빈 껍데기를 남기면 다음 실행이 "이미 채워졌다"고 건너뛴다.
                # 길이를 맞춰 zip하지 않는 이유: `user_names`는 빈 ID를 걸러내므로 결과가
                # 입력보다 짧을 수 있다. "풀린 이름은 원시 ID 목록에 없다"로 판정하면 된다.
                if any(name not in participants for name in names):
                    enriched["participant_names"] = names
                    stats["participants_resolved"] += 1
                    changed = True

            # 기본은 "비어 있을 때만 채운다"(멱등). `refresh_first_message`는 그걸 깨고
            # 다시 유도한다 - 발췌 선정 규칙이 바뀌면(예: 멤버십 이벤트 건너뛰기가 생기면)
            # 이미 쓰인 `first_message`는 낡은 규칙의 산물인데, 멱등성만 지키면 그 값이
            # 영원히 고쳐지지 않아 수정이 기존 설치에 닿지 못한다.
            # 재계산인데 쓸 만한 메시지가 하나도 없으면(문서 전체가 가입 알림인 경우 -
            # 현재 코퍼스 197건 중 34건이 그렇다) 낡은 값을 **지운다**. 남겨 두면 제목이
            # 옛 규칙이 고른 잡음을 계속 달고 있고, 그건 이 수정이 고치려던 바로 그 상태다.
            # 발췌가 없으면 제목은 "#채널 날짜"로 돌아간다 - 내용 0인 발췌보다 낫다.
            if refresh_first_message and not first_text and enriched.get("first_message"):
                del enriched["first_message"]
                stats["first_messages_filled"] += 1
                changed = True
            elif first_text and (refresh_first_message or not enriched.get("first_message")):
                excerpt_source = directory.resolve_mentions(first_text)
                excerpt_source = " ".join(str(excerpt_source).split())
                if excerpt_source:
                    fresh = excerpt_source[:_SLACK_FIRST_MESSAGE_CHARS]
                    if fresh != enriched.get("first_message"):
                        enriched["first_message"] = fresh
                        stats["first_messages_filled"] += 1
                        changed = True

            if changed:
                source_updates[source_id] = enriched

            title = derive_document_title(
                metadata_title=metadata_title,
                source_type="slack",
                source_metadata=enriched,
                content_at=content_at,
                first_heading=first_heading,
            )
            if title is not None and title != current_title:
                title_updates.append((title, document_id))

        stats["channels_unresolved"] = len(unresolved_channels)
        if source_updates:
            async with self.conn.cursor() as cur:
                await cur.executemany(
                    "UPDATE sources SET metadata = %s WHERE source_id = %s",
                    [(Jsonb(value), source_id) for source_id, value in source_updates.items()],
                )
            stats["sources_updated"] = len(source_updates)
        if title_updates:
            async with self.conn.cursor() as cur:
                await cur.executemany(
                    "UPDATE documents SET title = %s WHERE document_id = %s", title_updates
                )
            stats["titles_updated"] = len(title_updates)
        return stats

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
        embedding. Match tiers, ranked in this order - the three SQL ones are
        each a pg_trgm-backed predicate on the one indexed column, and the
        token-set tier re-scores the rows they fetched:

        1. whole-query substring (`title ILIKE '%q%'`) - the user typed (part
           of) the exact name;
        1'. **토큰 집합 일치**(`title_match.match_titles`) - 구분자와 순서를 무시하고
           질의/제목을 토큰 집합으로 접어 비교한다. substring tier가 순서에 묶여 있어
           `"Dev Handover README"`가 `README_ProjectIntel_Dev_Handover.md`를 못 맞추던
           구멍을 메운다. 집합이 같거나(질의 == 제목), 후보 중 유일하게 질의 집합을 통째로
           담고 있으면 substring과 같은 1.0. 그런 문서가 여럿이면 0.8에 묶어 둔다 -
           `_claim_anchor`가 1.0 하나를 anchor로 확정해 다른 전략까지 그 문서로 좁히므로
           가짜 1.0의 대가가 크다(설계 근거 전부는 `storage/title_match.py` docstring);
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
        similarity for tier 3, and the token-set tier only ever **raises** a
        row's score (`max`), never lowers it - 점수를 내리면 anchor 격차 판정
        (`_ANCHOR_MIN_GAP`)이 넓어져 없던 anchor가 생긴다. 토큰 tier가 주는 1.0 미만
        점수의 상한이 0.8인 것도 같은 이유다: 1위가 1.0일 때 2위를 0.8 위로 올리지
        않으므로 이 tier는 있던 anchor를 깨뜨릴 수 없다. 정렬은 tier -> 점수 순이라
        토큰 tier가 아무것도 올리지 못한 질의의 결과는 이 tier가 없던 때와 동일하다. `since`/`until` filter on `COALESCE(s.modified_at,
        s.collected_at)` - the same real-content timestamp `search_by_embedding`
        uses (its docstring explains why not a pipeline timestamp).

        질의는 NFC로 정규화해서 쓴다(`text_normalize.py`): 사용자가 macOS/Google Drive에서
        복사한 NFD 파일명을 붙여넣어도 NFC로 저장된 제목과 만나야 한다. 적재 쪽 정규화와
        짝이라 한쪽만으로는 절반만 맞는다 - 저장만 고치면 NFD 질의가, 질의만 고치면 이미
        저장된 NFD 행이 남는다."""
        query = to_nfc(query)
        # 토큰 tier가 순위를 뒤집을 수 있으므로 SQL 순서만 믿고 자르면 안 된다 - 넉넉히 받아
        # 재채점한 뒤 파이썬에서 자른다(`_TITLE_TOKEN_OVERFETCH`).
        fetch_limit = min(limit * _TITLE_TOKEN_OVERFETCH, _TITLE_TOKEN_MAX_FETCH)
        params = _lexical_params(query)
        async with self._read_transaction(*_lexical_settings(_TITLE_SIMILARITY_THRESHOLD)) as cur:
            await cur.execute(
                f"""
                WITH hits AS MATERIALIZED ({_lexical_candidates_sql("documents", "document_id", "title", params)})
                SELECT d.document_id, d.title, d.source_id, s.source_type, s.uri, s.modified_at,
                       d.project_id, COALESCE(s.modified_at, s.collected_at) AS content_at,
                       (d.title ILIKE %(pattern)s) AS is_substring,
                       (d.title ILIKE ALL(%(tokens)s::text[])) AS all_tokens,
                       word_similarity(%(query)s, d.title) AS sim
                FROM hits
                JOIN documents d ON d.document_id = hits.document_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE d.title IS NOT NULL
                  AND (%(project_id)s::text IS NULL OR d.project_id = %(project_id)s)
                  AND (%(source_type)s::text IS NULL OR s.source_type = %(source_type)s)
                  AND (%(since)s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) >= %(since)s)
                  AND (%(until)s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) < %(until)s)
                ORDER BY is_substring DESC, all_tokens DESC, sim DESC, d.title, d.document_id
                LIMIT %(limit)s
                """,
                {
                    **params,
                    "project_id": project_id,
                    "source_type": source_type,
                    "since": since,
                    "until": until,
                    "limit": fetch_limit,
                },
            )
            rows = await cur.fetchall()

        # 토큰 집합 tier는 후보 전체를 봐야 판정된다("질의 토큰을 통째로 담은 문서가 하나뿐인가").
        # `match_titles`가 그 유일성까지 여기서 계산한다.
        token_matches = match_titles(query, [row[1] for row in rows], [bool(row[8]) for row in rows])
        hits: list[dict[str, Any]] = []
        for row, token_match in zip(rows, token_matches, strict=True):
            is_substring, all_tokens, sim = bool(row[8]), bool(row[9]), float(row[10])
            if is_substring:
                score = 1.0
            elif all_tokens:
                score = 0.5 + sim / 2
            else:
                score = sim
            # 토큰 tier는 **올리기만** 한다 - 기존 tier의 점수를 내리면 anchor 격차
            # (`_ANCHOR_MIN_GAP`)가 넓어져 없던 anchor가 생기는데, 그건 이 변경의 의도가 아니다.
            score = max(score, token_match.score)
            # 정렬용 tier - 점수만으로 정렬하면 기존 tier들 사이의 순서가 바뀐다(퍼지 tier의
            # 점수 `sim`은 all-tokens tier의 `0.5 + sim/2`보다 클 수 있는데, 지금 `ORDER BY`는
            # tier를 먼저 본다). tier를 1차 키로 두면 **토큰 tier가 아무것도 올리지 못한
            # 질의에서는 결과 순서가 이 변경 전과 정확히 같다** - 공유 DB에서 페어드 비교를
            # 할 때 움직인 질의가 이 변경 때문인지 아닌지를 구분할 수 있어야 한다.
            tier = 0 if score >= 1.0 else (1 if all_tokens or token_match.score > 0 else 2)
            hits.append(
                {
                    "_tier": tier,
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
        hits.sort(key=lambda hit: (hit["_tier"], -hit["score"]))
        for hit in hits:
            del hit["_tier"]
        return hits[:limit]

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
        would show nothing relevant). Every tier is fetched through the trigram
        index (`_lexical_candidates_sql`), and `word_similarity()` is computed
        only for those candidates - see that helper for why the result set is
        identical to the single `OR` predicate it replaced.

        질의 NFC 정규화 이유는 `search_documents_by_title` 참고 - `_lexical_snippet`의
        일치 지점 탐색도 같은 정규화된 질의를 쓴다."""
        query = to_nfc(query)
        params = _lexical_params(query)
        async with self._read_transaction(*_lexical_settings(_CHUNK_SIMILARITY_THRESHOLD)) as cur:
            await cur.execute(
                f"""
                WITH hits AS MATERIALIZED ({_lexical_candidates_sql("chunks", "chunk_id", "text", params)})
                SELECT c.chunk_id, c.document_id, c.source_id, c.hierarchy_level, c.sequence_index, c.text,
                       d.title, d.project_id, s.source_type, s.uri, s.modified_at,
                       COALESCE(s.modified_at, s.collected_at) AS content_at,
                       (c.text ILIKE %(pattern)s) AS is_substring,
                       word_similarity(%(query)s, c.text) AS sim
                FROM hits
                JOIN chunks c ON c.chunk_id = hits.chunk_id
                JOIN documents d ON d.document_id = c.document_id
                JOIN sources s ON s.source_id = d.source_id
                WHERE (%(project_id)s::text IS NULL OR d.project_id = %(project_id)s)
                  AND (%(since)s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) >= %(since)s)
                  AND (%(until)s::timestamptz IS NULL OR COALESCE(s.modified_at, s.collected_at) < %(until)s)
                ORDER BY is_substring DESC, sim DESC, c.document_id, c.sequence_index
                LIMIT %(limit)s
                """,
                {**params, "project_id": project_id, "since": since, "until": until, "limit": limit},
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
# `backfill_slack_display_names`가 `sources.metadata.first_message`에 실어 두는 원문 길이 상한.
# `context/slack.py::_FIRST_MESSAGE_CHARS`와 같은 값이어야 한다 - 수집 경로와 백필 경로가
# 같은 모양의 메타데이터를 남겨야 `db backfill-titles --overwrite`가 두 경로에서 같은 제목을
# 낸다. (상수를 import하지 않는 이유: 스토리지 계층이 context 계층을 참조하면 의존이 뒤집힌다.)
_SLACK_FIRST_MESSAGE_CHARS = 400
# 발췌 후보로 훑어볼 앞쪽 텍스트 블록 수. 가입 알림이 연달아 오는 최대 길이를 덮으면
# 되고(관측 최대 5줄), 그 뒤까지 뒤지면 "첫 메시지"라고 부를 수 없는 것을 싣게 된다.
_SLACK_EXCERPT_CANDIDATES = 8
_CHUNK_SIMILARITY_THRESHOLD = 0.5
# Cap on how many whitespace tokens the all-tokens tier ANDs together - past
# this a query is a sentence, not a name, and belongs to vector_search.
_MAX_QUERY_TOKENS = 8
# 제목 조회는 `limit`보다 이만큼 더 받아 와서 토큰 집합 tier로 재채점한 뒤 자른다. 토큰
# tier는 SQL의 `ORDER BY`가 뒤로 보낸 행을 1.0까지 끌어올릴 수 있어서(순서가 다른 파일명이
# `word_similarity`만으로 걸린 경우), SQL이 자른 뒤에 채점하면 그 행이 애초에 없다.
# 유일성 판정(`title_match.match_titles`)도 후보를 넓게 봐야 정확해진다. 상한을 둔 이유는
# exhaustive 질의의 limit이 200이라 곱하면 조회가 커지기 때문.
_TITLE_TOKEN_OVERFETCH = 4
_TITLE_TOKEN_MAX_FETCH = 200


# "Not superseded" - the current head of a claim's version chain (see
# list_knowledge_objects_by_source for the definition), for reads that
# should only see current claims. Served by idx_ko_supersedes.
_HEAD_KO = """NOT EXISTS (
    SELECT 1 FROM knowledge_objects newer WHERE newer.supersedes = ko.knowledge_object_id
)"""

# Column order `_ko_from_row` reads - every KnowledgeObject read path selects
# exactly this, joined to `sources s` for the SourceRef fields.
_KO_COLUMNS = """ko.knowledge_object_id, ko.type, ko.document_id, ko.evidence, ko.summary,
                       ko.content, ko.topics, ko.document_type, ko.extracted_at, ko.model_used,
                       ko.version, ko.pipeline_version, ko.source_hash, ko.supersedes,
                       s.source_id, s.source_type, s.uri, s.project_id, ko.confidence"""


def _ko_from_row(row: tuple[Any, ...]) -> KnowledgeObject:
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
        confidence=row[18],
    )


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


def _lexical_params(query: str) -> dict[str, Any]:
    """`_lexical_candidates_sql`과 두 검색 SELECT가 공유하는 이름 붙은 파라미터. 토큰
    패턴은 배열(`tokens`, 점수 계산의 `ILIKE ALL`)과 낱개(`token0`.., 후보 분기의 AND)로 둘 다 싣는다."""
    tokens = _token_patterns(query)
    return {
        "query": query,
        "pattern": _like_pattern(query),
        "tokens": tokens,
        **{f"token{i}": token for i, token in enumerate(tokens)},
    }


def _lexical_settings(threshold: float) -> tuple[str, str]:
    """후보 분기의 `<%`가 읽는 임계값, 그리고 순차 스캔 금지 - 둘 다 `SET LOCAL`이라 이 검색
    한 건의 트랜잭션에만 걸린다. `enable_seqscan = off`가 필요한 이유: 이 규모(청크 수천 행)에서는
    플래너가 `word_similarity`의 행당 비용(긴 본문의 trigram 생성)을 모른 채 `<%` 분기를
    순차 스캔(행마다 유사도 계산, ~350ms)으로 고른다. 플래너 설정은 계획만 바꾸고 결과는 못
    바꾸며, 인덱스가 못 쓰이는 분기(trigram 없는 2글자 토큰 등)는 여전히 전체 인덱스 스캔으로 답한다."""
    return (f"SET LOCAL pg_trgm.word_similarity_threshold = {float(threshold)}", "SET LOCAL enable_seqscan = off")


def _lexical_candidates_sql(table: str, key: str, column: str, params: dict[str, Any]) -> str:
    """세 매칭 tier(전체 substring / 모든 토큰 substring / `word_similarity` >= 임계값)를
    만족하는 행의 `key`를 tier마다 따로 trigram GIN 인덱스로 뽑아 합친다.

    예전의 `A OR ILIKE ALL(...) OR word_similarity(q, col) >= t` 한 WHERE는 인덱스를 못 탔다 -
    `word_similarity()` 호출은 인덱스 연산자가 아니고 `ILIKE ALL(array)`도 GIN이 받지 못해,
    행마다 유사도를 계산하는 순차 스캔이 됐다. 분기별로 다시 쓰면 결과 집합이 정확히 같다:
    - fuzzy: `q <% col`은 `word_similarity(q, col) >= pg_trgm.word_similarity_threshold`와
      같은 판정이다(연산자가 같은 함수를 부르고, 임계값을 넘는 순간 멈추는 최적화만 있음).
      임계값은 `_lexical_settings`가 트랜잭션에 건다.
    - 모든 토큰: `ILIKE ALL(array)`를 토큰별 `ILIKE ... AND`로 펼친다 - 같은 술어이고 GIN이 받는다.
      토큰이 전체 패턴 하나뿐이면(공백 없는 질의) 전체 substring 분기와 같아 생략한다.
    - `ILIKE`/`AND` 분기는 trigram이 없는 패턴(2글자 이하 토큰)에서도 GIN 전체 스캔 +
      재검사로 정확히 답한다 - 행이 빠지지 않고 느려질 뿐이다.
    OR 하나로 합치지 않고 UNION으로 나누는 이유: BitmapOr는 재검사 때 OR 전체를 평가해,
    한 분기가 trigram 없는 패턴이면 모든 행에 `word_similarity`를 계산하는 예전 비용으로 돌아간다."""
    branches = [f"{column} ILIKE %(pattern)s", f"%(query)s <%% {column}"]
    if params["tokens"] != [params["pattern"]]:
        branches.append(" AND ".join(f"{column} ILIKE %(token{i})s" for i in range(len(params["tokens"]))))
    return " UNION ".join(f"SELECT {key} FROM {table} WHERE {branch}" for branch in branches)


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
