"""벡터 경로 엔드투엔드 통합 테스트 - 임베딩 행이 실제로 있는 DB에서
`record_chunk_embeddings`/`record_embeddings` → `PostgresStore.search_by_embedding`
→ `search/service.py::vector_search` → `search/retrieve.py::retrieve`의 RRF 융합까지가
정말로 동작하는지 확인한다.

왜 따로 있나: `tests/search/test_vector_search.py`는 `vector_search`가 인자를 store에
넘기는지만 보는 가짜-store 단위 테스트이고, `tests/search/test_retrieve.py`는 벡터를
전부 monkeypatch로 대체한다. 그래서 "임베딩 행이 실제로 채워졌을 때" 경로 -
`vector` 타입 바인딩, 코사인 거리→유사도 부호, `target_kind`/`embedding_model`/
`project_id` 필터, 청크 히트의 `summary` 절단, 그리고 벡터 히트가 최종 랭킹에
반영되는지 - 는 어느 쪽도 건드리지 않는다. 개발 DB의
`knowledge_object_embeddings`가 0행이라 골든셋 76개 질의 전부에서 `vector_search`가
0히트였고, 그 상태에서는 이 경로 전체가 한 번도 실행된 적이 없다.

임베딩 API 키 없이 돌아야 하므로 임베더는 `ConceptEmbedder`(아래) - 텍스트에 어떤
개념 어휘가 들어 있는지로 축을 고르는 결정론적 1536차원 단위벡터다. 순수 해시 임베더가
아닌 이유: 이 파일의 핵심 검증이 "표면형이 전혀 겹치지 않는 질의를 벡터가 잡아내는가"라
질의와 문서가 같은 방향을 가리키게 만들 수 있어야 한다. 해시는 그걸 못 한다.

DB 규약은 tests/storage/test_lexical_store.py와 같다 - 전용 project_id + `test-vp-`
접두사로만 쓰고 teardown에서 지운다(개발 DB에 실데이터가 들어 있다).
"""

from __future__ import annotations

import hashlib
import math
import os
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from knowledge_forge.config import settings
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import (
    DocumentRef,
    Evidence,
    KnowledgeObject,
    KnowledgeObjectType,
)
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.search.retrieve import retrieve
from knowledge_forge.search.service import vector_search
from knowledge_forge.storage.postgres_store import PostgresStore
from knowledge_forge.storage.store import PipelineStore

DSN = os.environ.get("KF_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")

_DIM = 1536  # schema.sql의 vector(1536)
_PROJECT = "test-vecpath"
_OTHER_PROJECT = "test-vecpath-other"
_MODEL = "test-vecpath-model"
_OTHER_MODEL = "test-vecpath-model-b"
_T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
_NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)

# 개념 어휘 → 축 번호. 같은 축이면 코사인 유사도 1.0, 다른 축이면 0.0이 되도록 축은 서로
# 직교하는 표준기저다. 축 번호는 앞쪽(0..)에 몰아두고, 어휘에 걸리지 않는 텍스트는 뒤쪽
# 64개 축 중 하나로 보내 개념 축과 절대 겹치지 않게 한다.
_CONCEPTS: dict[str, int] = {
    "연차": 0,
    "휴가": 0,
    "쉬는": 0,
    "배포": 1,
    "롤백": 1,
    "예산": 2,
}


class ConceptEmbedder:
    """결정론적·API 불필요 `EmbeddingProvider` 구현 - 텍스트에 들어 있는 개념 어휘의
    축들을 켠 뒤 L2 정규화한 1536차원 단위벡터를 돌려준다. 같은 개념끼리는 코사인
    유사도 1.0, 다른 개념끼리는 0.0이라 "무엇이 무엇보다 위에 와야 하는가"를 부동소수점
    여유 없이 단정할 수 있다."""

    def __init__(self, dim: int = _DIM) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def vector_for(self, text: str) -> list[float]:
        axes = sorted({axis for word, axis in _CONCEPTS.items() if word in text})
        vec = [0.0] * self.dim
        if not axes:
            # 개념 어휘가 하나도 없는 텍스트: 텍스트마다 고정된, 개념 축과 직교하는 방향.
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            vec[self.dim - 1 - int(digest, 16) % 64] = 1.0
            return vec
        norm = math.sqrt(len(axes))
        for axis in axes:
            vec[axis] = 1.0 / norm
        return vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector_for(text) for text in texts]


def _source(idx: int, *, project_id: str = _PROJECT, name: str) -> Source:
    return Source(
        source_id=f"test-vp-s{idx}",
        source_type=SourceType.GOOGLE_DRIVE,
        uri=f"test://vp/{idx}",
        content_hash=f"h-vp-{idx}",
        collected_at=_T0,
        modified_at=_T0,
        metadata={"name": name},
        project_id=project_id,
    )


def _document(idx: int, src: Source, title: str) -> Document:
    return Document(
        document_id=f"test-vp-d{idx}",
        source=SourceRef.from_source(src),
        elements=[TextBlock(element_id=f"e{idx}", text=title)],
        metadata=DocumentMetadata(title=title),
    )


def _chunk(idx: int, doc: Document, text: str) -> Chunk:
    return Chunk(
        chunk_id=f"test-vp-c{idx}",
        document_id=doc.document_id,
        source_id=doc.source.source_id,
        hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=0,
        text=text,
        token_count=len(text.split()),
        element_ids=[f"e{idx}"],
    )


# 표면형이 겹치지 않게 고른 문장들 - "쉬는 날 규정"(질의)은 d1의 본문/제목 어디에도
# substring으로 존재하지 않으므로, d1이 결과에 들어오면 그건 벡터 경로가 잡은 것이다.
_D1_TEXT = "연차 신청은 인사팀 결재를 거쳐 처리됩니다."
_D2_TEXT = "배포 파이프라인에서 오류가 나면 롤백 절차를 따릅니다."
_D3_TEXT = "예산 편성 원칙 " + "세부 항목 검토 " * 120  # > 500자, summary 절단 확인용
_D4_TEXT = "연차 관련 다른 프로젝트의 문서입니다."
_KO_SUMMARY = "연차 사용 규정 안내"

_LEAVE_QUERY = "쉬는 날 규정"


async def _seed(db: PostgresStore) -> None:
    s1 = _source(1, name="인사팀 안내문")
    s2 = _source(2, name="릴리스 노트")
    s3 = _source(3, name="예산 계획")
    s4 = _source(4, project_id=_OTHER_PROJECT, name="타 프로젝트 안내")
    d1, d2, d3, d4 = (
        _document(1, s1, "인사팀 안내문"),
        _document(2, s2, "릴리스 노트"),
        _document(3, s3, "예산 계획"),
        _document(4, s4, "타 프로젝트 안내"),
    )
    for src in (s1, s2, s3, s4):
        await db.save_source(src, raw_path=None)
    for doc in (d1, d2, d3, d4):
        await db.save_document(doc)
    await db.save_chunks([_chunk(1, d1, _D1_TEXT), _chunk(2, d2, _D2_TEXT), _chunk(3, d3, _D3_TEXT), _chunk(4, d4, _D4_TEXT)])
    await db.save_knowledge_objects(
        [
            KnowledgeObject(
                knowledge_object_id="test-vp-ko1",
                type=KnowledgeObjectType.POLICY,
                source=SourceRef.from_source(s1),
                document=DocumentRef(document_id=d1.document_id, title="인사팀 안내문"),
                evidence=[
                    Evidence(
                        chunk_id="test-vp-c1",
                        document_id=d1.document_id,
                        hierarchy_level=HierarchyLevel.PARAGRAPH,
                        sequence_index=0,
                    )
                ],
                summary=_KO_SUMMARY,
                content={"statement": _KO_SUMMARY},
                topics=["인사"],
                document_type="policy",
                extracted_at=_T0,
                model_used="test",
                pipeline_version="0.1.0",
                source_hash="h-vp-1",
            )
        ]
    )


async def _cleanup(db: PostgresStore) -> None:
    await db.conn.execute("DELETE FROM knowledge_object_embeddings WHERE project_id IN (%s, %s)", (_PROJECT, _OTHER_PROJECT))
    await db.conn.execute("DELETE FROM knowledge_objects WHERE knowledge_object_id LIKE 'test-vp-%'")
    await db.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'test-vp-%'")
    await db.conn.execute("DELETE FROM document_links WHERE from_document_id LIKE 'test-vp-%' OR to_document_id LIKE 'test-vp-%'")
    await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-vp-%'")
    await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-vp-%'")


@pytest_asyncio.fixture
async def embedder(monkeypatch):
    """`vector_search`가 요청 시점에 부르는 `get_embedding_provider`를 결정론적
    임베더로 바꾸고, 기본 임베딩 모델 이름도 이 테스트 전용으로 돌린다 - 모델 이름은
    `knowledge_object_embeddings`의 사실상 파티션 키라(검색도 `embedding_model =`로
    거른다) 저장과 검색이 같은 이름을 써야 한다."""
    provider = ConceptEmbedder()
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: provider)
    monkeypatch.setattr(settings, "embedding_model", _MODEL)
    return provider


@pytest_asyncio.fixture
async def store(tmp_path, embedder):
    """청크 4개 + KO 1개를 심고 청크/KO 임베딩까지 기록한 `PipelineStore`."""
    async with PostgresStore(DSN) as db:
        await db.init_schema()
        await _cleanup(db)  # 이전 실패 실행이 남긴 행이 있으면 먼저 치운다
        await _seed(db)
    async with PipelineStore(DSN, tmp_path) as pipeline_store:
        chunks = {c.chunk_id: c for c in await pipeline_store.list_chunks_by_document("test-vp-d1")}
        chunks.update({c.chunk_id: c for c in await pipeline_store.list_chunks_by_document("test-vp-d2")})
        chunks.update({c.chunk_id: c for c in await pipeline_store.list_chunks_by_document("test-vp-d3")})
        await pipeline_store.record_chunk_embeddings(
            [chunks["test-vp-c1"], chunks["test-vp-c2"], chunks["test-vp-c3"]], _PROJECT, embedder, _MODEL
        )
        other = await pipeline_store.list_chunks_by_document("test-vp-d4")
        await pipeline_store.record_chunk_embeddings(other, _OTHER_PROJECT, embedder, _MODEL)
        objects = await pipeline_store.list_prior_knowledge_objects("test-vp-s1")
        await pipeline_store.record_embeddings(objects, embedder, _MODEL)
        try:
            yield pipeline_store
        finally:
            async with PostgresStore(DSN) as db:
                await _cleanup(db)


# --- store/vector_search 계층 ---------------------------------------------------


async def test_chunk_embeddings_are_found_by_a_query_with_no_surface_overlap(store, embedder):
    """핵심 검증: 질의 "쉬는 날 규정"은 d1의 제목에도 본문에도 substring으로 없다.
    그래도 1위로 나온다면 임베딩 저장(`vector` 바인딩) → HNSW 조회 → 코사인 거리
    →유사도 변환까지가 실제로 이어져 있다는 뜻이다."""
    assert _LEAVE_QUERY not in _D1_TEXT
    assert all(word not in _D1_TEXT for word in _LEAVE_QUERY.split())

    hits = await vector_search(_LEAVE_QUERY, store, project_id=_PROJECT, limit=10, target_kind="chunk")

    assert [h["chunk_id"] for h in hits][0] == "test-vp-c1"
    top = hits[0]
    assert top["document_id"] == "test-vp-d1"
    assert top["target_kind"] == "chunk"
    assert top["knowledge_object_id"] is None
    assert top["type"] == "chunk"
    assert top["project_id"] == _PROJECT
    assert top["content_at"] == _T0.isoformat()
    # 코사인 거리 0 → 유사도 1.0. 부호가 뒤집혀 있으면 여기서 걸린다.
    assert top["score"] == pytest.approx(1.0)
    # 개념이 다른 청크는 직교(유사도 0)이고, 절대 1위보다 위에 오지 않는다.
    assert all(h["score"] < top["score"] for h in hits[1:])
    assert hits[-1]["score"] == pytest.approx(0.0, abs=1e-6)


async def test_chunk_hit_summary_is_the_embedded_text_truncated(store):
    hits = await vector_search("예산 편성", store, project_id=_PROJECT, limit=10, target_kind="chunk")

    top = hits[0]
    assert top["chunk_id"] == "test-vp-c3"
    assert len(_D3_TEXT) > 500
    assert len(top["summary"]) == 500
    assert top["summary"] == " ".join(_D3_TEXT.split())[:500]


async def test_target_kind_narrows_the_grain_and_none_mixes_both(store):
    """`knowledge_object` grain도 같은 벡터 공간에서 조회된다 - KO 히트는 KO의 summary와
    type을, 청크 히트는 청크 원문과 'chunk'를 싣는다."""
    chunk_only = await vector_search(_LEAVE_QUERY, store, project_id=_PROJECT, limit=10, target_kind="chunk")
    ko_only = await vector_search(_LEAVE_QUERY, store, project_id=_PROJECT, limit=10, target_kind="knowledge_object")
    both = await vector_search(_LEAVE_QUERY, store, project_id=_PROJECT, limit=10)

    assert {h["target_kind"] for h in chunk_only} == {"chunk"}
    assert [h["knowledge_object_id"] for h in ko_only] == ["test-vp-ko1"]
    assert ko_only[0]["summary"] == _KO_SUMMARY
    assert ko_only[0]["type"] == "policy"
    assert ko_only[0]["chunk_id"] is None
    assert ko_only[0]["document_id"] == "test-vp-d1"
    assert ko_only[0]["score"] == pytest.approx(1.0)
    # grain을 섞으면 둘 다 후보다 - 같은 문서를 두 grain이 각각 뒷받침한다.
    assert {"test-vp-c1", None} <= {h["chunk_id"] for h in both}
    assert "test-vp-ko1" in {h["knowledge_object_id"] for h in both}
    assert len(both) == len(chunk_only) + len(ko_only)


async def test_project_scope_is_enforced_on_the_vector_path(store):
    """다른 프로젝트의 청크(test-vp-c4)는 같은 개념 축이라 유사도가 1위와 똑같다 -
    스코프가 안 걸리면 반드시 결과에 섞인다."""
    scoped = await vector_search(_LEAVE_QUERY, store, project_id=_PROJECT, limit=10)
    unscoped = await vector_search(_LEAVE_QUERY, store, limit=50)

    assert "test-vp-c4" not in {h["chunk_id"] for h in scoped}
    assert {h["project_id"] for h in scoped} == {_PROJECT}
    assert "test-vp-c4" in {h["chunk_id"] for h in unscoped}


async def test_a_different_models_vectors_never_enter_the_ranking(store, embedder):
    """`knowledge_object_embeddings`는 모델별로 여러 벡터를 동시에 들고 있을 수 있다
    (schema.sql). 검색이 `embedding_model`로 거르지 않으면 다른 벡터 공간의 행이
    섞여 들어온다 - 여기서는 d2 청크에 "휴가" 방향 벡터를 다른 모델 이름으로 하나 더
    심어 두고, 현재 모델로 검색했을 때 그게 1위를 뺏지 않는지 본다."""
    from knowledge_forge.embedding.base import EmbeddingRecord

    async with PostgresStore(DSN) as db:
        await db.save_embeddings(
            [
                EmbeddingRecord(
                    target_kind="chunk",
                    chunk_id="test-vp-c2",
                    document_id="test-vp-d2",
                    embedding_model=_OTHER_MODEL,
                    embedding=embedder.vector_for("연차"),
                    embedded_text="연차",
                    embedded_text_hash="h-other-model",
                    project_id=_PROJECT,
                )
            ]
        )

    hits = await vector_search(_LEAVE_QUERY, store, project_id=_PROJECT, limit=10, target_kind="chunk")
    other = await vector_search(
        _LEAVE_QUERY, store, project_id=_PROJECT, limit=10, target_kind="chunk", embedding_model=_OTHER_MODEL
    )

    assert hits[0]["chunk_id"] == "test-vp-c1"
    # c2와 c3는 둘 다 질의와 직교(거리 동점)라 둘 사이 순서는 정해져 있지 않다 - 쿼리에 tiebreak를 넣으면
    # 플래너가 HNSW를 버리므로 테스트가 순서를 묻지 않는다.
    assert {h["chunk_id"] for h in hits[1:]} == {"test-vp-c2", "test-vp-c3"}
    assert all(h["score"] == pytest.approx(0.0, abs=1e-6) for h in hits[1:])  # d2는 여전히 직교 - 다른 모델 행이 안 섞였다
    # 그 모델로 명시해서 물으면 그 행만 나온다.
    assert [h["chunk_id"] for h in other] == ["test-vp-c2"]
    assert other[0]["score"] == pytest.approx(1.0)


async def test_time_filter_applies_to_vector_hits(store):
    before = await vector_search(
        _LEAVE_QUERY, store, project_id=_PROJECT, limit=10, until=datetime(2026, 7, 1, tzinfo=timezone.utc)
    )
    within = await vector_search(
        _LEAVE_QUERY, store, project_id=_PROJECT, limit=10, since=datetime(2026, 7, 1, tzinfo=timezone.utc)
    )

    assert before == []
    assert "test-vp-c1" in {h["chunk_id"] for h in within}


# --- retrieve()의 RRF 융합 -------------------------------------------------------


async def test_vector_only_run_ranks_the_semantically_matching_document_first(store):
    result = await retrieve(_LEAVE_QUERY, store=store, project_id=_PROJECT, now=_NOW, strategies_override=["vector"])

    # `["vector"]`는 진짜 벡터 단독이다 - 어휘 arm이 이름 없이 `vector()` 안에 붙어 있던
    # 동안에는 여기에 document_search#semantic/lexical_search#semantic도 섞여 있었다.
    assert {s.source for s in result.steps} == {"vector_search"}
    step = next(s for s in result.steps if s.strategy == "vector")
    assert step.detail["error"] is None
    assert step.n_hits > 0
    assert result.document_ids[0] == "test-vp-d1"
    assert result.hits[0]["retrieval_sources"] == ["vector_search"]


async def test_vector_hit_survives_rrf_next_to_a_lexical_only_hit(store):
    """어휘 검색이 전혀 못 잡는 문서(d1)와 벡터가 전혀 못 잡는 문서(d2)를 한 질의에
    섞는다: 따옴표 구절 "롤백 절차"는 d2 본문에만 있고(lexical), 나머지 "쉬는 날 규정"은
    d1과 개념이 같다(vector). 최종 랭킹에 둘 다 남아야 벡터 히트가 RRF를 통과한 것이다."""
    result = await retrieve(f'"롤백 절차" {_LEAVE_QUERY}', store=store, project_id=_PROJECT, now=_NOW)

    assert result.strategies == ["lexical", "vector", "keyword_arm"]
    lexical = next(s for s in result.steps if s.strategy == "lexical")
    vector = next(s for s in result.steps if s.strategy == "vector")
    assert vector.detail["query"] == _LEAVE_QUERY
    # 어휘 쪽은 d2만 안다 - d1이 결과에 있다면 그건 벡터가 넣은 것이다.
    assert {h["document_id"] for h in store_hits(result, "lexical_search")} == {"test-vp-d2"}
    assert lexical.n_hits >= 1
    assert vector.n_hits >= 1
    assert set(result.document_ids) >= {"test-vp-d1", "test-vp-d2"}
    d1_hit = next(h for h in result.hits if h["document_id"] == "test-vp-d1")
    assert d1_hit["retrieval_sources"] == ["vector_search"]
    # 두 소스가 함께 지목한 d2가 1위(RRF 가산), 벡터 단독인 d1이 그 뒤.
    assert result.document_ids[0] == "test-vp-d2"


def store_hits(result, source: str) -> list[dict]:
    """소스 계열(`#` 앞 이름)로 거른다 - 질의·term별 리스트는 `lexical_search#<질의>`처럼 이름이 갈린다."""
    return [h for h in result.hits if any(s.split("#")[0] == source for s in h.get("retrieval_sources", []))]


async def test_keyword_arm_runs_alongside_vector_once_embeddings_exist(store):
    """이 테스트는 원래 정반대를 못 박고 있었다: `_keyword_fallback`은 vector가 0히트일 때만
    도는 degrade라, 임베딩을 채우면 ANN이 늘 `limit`개를 돌려줘 **영원히 안 돈다**는 것.
    그 예측은 맞았고, v0 골든셋에서 실제로 ndcg@10 0.865 → 0.804 회귀로 나타났다
    (run 9ad2bba2). `steps`를 열어 보니 "Dev Handover README" 같은 질의가 vector 단독
    30히트만으로 랭킹되고 있었다 - 0.865를 만든 어휘 경로가 통째로 꺼진 것이다.

    그래서 fallback을 하이브리드 arm으로 승격시켰다(retrieve.py::_Run.keyword_arm). 이제
    vector가 히트를 돌려줘도 어휘 조회가 같이 돌고 RRF에 별도 투표자로 들어간다.

    arm은 이제 이름 있는 전략이라 override에 명시해야 돈다 - `["vector"]`는 벡터 단독이다
    (바로 위 `test_vector_only_run_...`). 기본 라우팅은 여전히 이 둘을 함께 고른다."""
    result = await retrieve(
        _LEAVE_QUERY, store=store, project_id=_PROJECT, now=_NOW, strategies_override=["vector", "keyword_arm"]
    )

    arm_sources = [s.source for s in result.steps if s.detail.get("arm") == "semantic_query"]
    assert arm_sources == ["document_search#semantic", "lexical_search#semantic"]
    # 옛 이름은 남아 있으면 안 된다 - fallback이 아니라 상시 arm이다.
    assert all("fallback_from" not in s.detail for s in result.steps if s.detail.get("mode") != "per_term")


async def test_vector_min_score_drops_hits_for_a_query_nothing_matches(store):
    """ANN에는 "관련도 하한"이 없다 - 코퍼스에 전혀 없는 주제를 물어도 가장 가까운 `limit`개를
    무조건 돌려준다. exhaustive/count/negative는 컷오프 없이 그 후보 집합을 그대로 답으로 쓰기
    때문에, 임베딩을 채우자 v0에서 none_correct 1.000 → 0.000, count_match 0.333 → 0.000이
    됐다(run 9ad2bba2).

    `KF_VECTOR_MIN_SCORE`(config.py, 기본 0.45)가 그 하한이다. 임계값은 v0 스윕으로 정했고
    0.42~0.47이 고원이다 - 근거는 config.py 주석. 여기서는
    "하한이 실제로 후보를 버리고 그 사실이 steps에 남는다"는 계약만 고정한다."""
    result = await retrieve("단풍 낚시 여행 메시지 전부", store=store, project_id=_PROJECT, now=_NOW)

    vector = next(s for s in result.steps if s.strategy == "vector")
    assert vector.detail["min_score"] == settings.vector_min_score
    assert vector.detail["dropped_below_min_score"] > 0
    assert vector.n_hits == 0
