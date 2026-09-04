"""임베딩 적재 진단(`embedding_coverage`)과 필터링 ANN 설정(`hnsw.iterative_scan`)
통합 테스트 - `KF_TEST_DATABASE_URL`이 있을 때만 돈다(tests/storage의 다른 파일과 같은 규약).

`hnsw.iterative_scan` 쪽은 세션 GUC가 실제로 켜져 있는지만 본다: 이 설정이 없을 때
나는 증상(필터가 소수 행만 고르면 `search_by_embedding`이 히트를 통째로 잃는다)은
플래너가 HNSW 인덱스를 order-by 경로로 골라야 재현되고, 그건 테이블에 수천 행이
있을 때만 일어난다 - 테스트 하나가 그만큼을 심었다 지우는 건 너무 비싸다. 재현
측정치와 근거는 `PostgresStore._configure_ann_search`의 docstring과
wiki/search-architecture.md "벡터 경로"에 적어 두었다.
"""

import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from knowledge_forge.embedding.base import EmbeddingRecord
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.postgres_store import PostgresStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

_PROJECT = "test-embcov"
_MODEL = "test-embcov-model"
_T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def store():
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        yield db
        await db.conn.execute("DELETE FROM knowledge_object_embeddings WHERE project_id = %s", (_PROJECT,))
        await db.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'test-ec-%'")
        await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-ec-%'")
        await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-ec-%'")


async def _seed(db: PostgresStore) -> None:
    src = Source(
        source_id="test-ec-s1",
        source_type=SourceType.LOCAL_FILE,
        uri="file:///ec.md",
        content_hash="h-ec",
        collected_at=_T0,
        modified_at=_T0,
        project_id=_PROJECT,
    )
    doc = Document(
        document_id="test-ec-d1",
        source=SourceRef.from_source(src),
        elements=[TextBlock(element_id="e1", text="본문")],
        metadata=DocumentMetadata(title="진단용 문서"),
    )
    await db.save_source(src, raw_path=None)
    await db.save_document(doc)
    await db.save_chunks(
        [
            Chunk(
                chunk_id=f"test-ec-c{i}",
                document_id="test-ec-d1",
                source_id="test-ec-s1",
                hierarchy_level=HierarchyLevel.PARAGRAPH,
                sequence_index=i,
                text=f"청크 {i}",
                token_count=2,
                element_ids=["e1"],
            )
            for i in range(3)
        ]
    )


async def test_iterative_scan_is_enabled_on_every_connection(store):
    """필터가 소수 행만 고를 때 HNSW 스캔이 조기 종료해 히트를 잃는 걸 막는 설정
    (`_configure_ann_search`) - `search_by_embedding`은 항상 project_id로, 종종
    since/until/target_kind로도 거르기 때문에 이게 꺼져 있으면 안 된다."""
    row = await (await store.conn.execute("SHOW hnsw.iterative_scan")).fetchone()

    assert row is not None
    assert row[0] != "off"


async def test_embedding_coverage_reports_model_grain_and_backfill_progress(store):
    await _seed(store)
    await store.save_embeddings(
        [
            EmbeddingRecord(
                target_kind="chunk",
                chunk_id=f"test-ec-c{i}",
                document_id="test-ec-d1",
                embedding_model=_MODEL,
                embedding=[0.0] * 1535 + [1.0],
                embedded_text=f"청크 {i}",
                embedded_text_hash=f"h{i}",
                project_id=_PROJECT,
            )
            for i in range(2)  # 3개 중 2개만 - 백필 진행률이 드러나야 한다
        ]
    )

    coverage = await store.embedding_coverage(_MODEL, _PROJECT)

    assert coverage["rows_by_model_and_kind"] == {_MODEL: {"chunk": 2}}
    assert (coverage["chunks_total"], coverage["chunks_embedded"]) == (3, 2)
    assert coverage["dimensions"] == [1536]


async def test_embedding_coverage_shows_rows_stored_under_another_model_name(store):
    """벡터 검색 0히트의 가장 흔한 원인: `knowledge-forge embed`가 쓴 모델 이름과
    `KF_EMBEDDING_MODEL`이 다르다. 그때 검색은 아무 말 없이 0행이지만 진단은 "다른
    이름으로는 들어 있다"를 보여줘야 한다."""
    await _seed(store)
    await store.save_embeddings(
        [
            EmbeddingRecord(
                target_kind="chunk",
                chunk_id="test-ec-c0",
                document_id="test-ec-d1",
                embedding_model="test-embcov-other",
                embedding=[0.0] * 1535 + [1.0],
                embedded_text="청크 0",
                embedded_text_hash="h0",
                project_id=_PROJECT,
            )
        ]
    )

    coverage = await store.embedding_coverage(_MODEL, _PROJECT)

    assert coverage["chunks_embedded"] == 0  # 현재 모델 기준으로는 하나도 없고
    assert coverage["rows_by_model_and_kind"] == {"test-embcov-other": {"chunk": 1}}  # 다른 이름으로는 있다
    assert coverage["dimensions"] == []
