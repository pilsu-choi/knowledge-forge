"""`embedding/cache.py`의 유한 LRU와, `vector_search`가 그걸 실제로 쓰는지.

왜 이 테스트가 있나: 질의 임베딩 캐시는 지연시간을 위한 것이지만 틀리면 **조용히**
틀린다. 모델 이름을 키에서 빼먹으면 모델 A의 벡터로 모델 B의 파티션을 조회하게 되고
(`PostgresStore.search_by_embedding`의 docstring: "이 이름은 사실상 파티션 키다") 에러
없이 0히트나 엉뚱한 랭킹이 나온다. 상한이 안 걸리면 장수 프로세스에서 그냥 새는 dict다.
"""

from knowledge_forge.embedding.cache import BoundedEmbeddingCache, query_embedding_cache
from knowledge_forge.search.service import vector_search


class _FakeEmbedder:
    def __init__(self, vector):
        self._vector = vector
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [self._vector for _ in texts]


class _FakeStore:
    def __init__(self):
        self.embeddings: list[list[float]] = []
        self.models: list[str] = []

    async def search_by_embedding(self, embedding, embedding_model, **kwargs):
        self.embeddings.append(embedding)
        self.models.append(embedding_model)
        return []


def test_cache_hits_on_the_same_model_and_text():
    cache = BoundedEmbeddingCache(max_entries=4)
    cache.put("m", "질의", [1.0, 2.0])

    assert cache.get("m", "질의") == [1.0, 2.0]


def test_cache_never_crosses_embedding_models():
    """모델 이름은 키의 일부여야 한다 - 벡터 공간이 다르고, 검색도 `embedding_model =`로
    행을 거르기 때문에 섞이면 에러 없이 틀린 결과가 된다."""
    cache = BoundedEmbeddingCache(max_entries=4)
    cache.put("model-a", "질의", [1.0, 2.0])

    assert cache.get("model-b", "질의") is None


def test_cache_evicts_least_recently_used_beyond_the_bound():
    cache = BoundedEmbeddingCache(max_entries=2)
    cache.put("m", "a", [1.0])
    cache.put("m", "b", [2.0])
    cache.get("m", "a")  # a를 최근 사용으로 올린다
    cache.put("m", "c", [3.0])

    assert len(cache) == 2
    assert cache.get("m", "b") is None
    assert cache.get("m", "a") == [1.0]
    assert cache.get("m", "c") == [3.0]


def test_cache_returns_a_copy_so_callers_cannot_corrupt_it():
    cache = BoundedEmbeddingCache(max_entries=2)
    cache.put("m", "a", [1.0, 2.0])

    got = cache.get("m", "a")
    assert got is not None
    got[0] = 99.0

    assert cache.get("m", "a") == [1.0, 2.0]


async def test_vector_search_reuses_the_cached_query_embedding(monkeypatch):
    embedder = _FakeEmbedder([0.1, 0.2])
    store = _FakeStore()
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: embedder)

    await vector_search("배포 일정", store)
    await vector_search("배포 일정", store)

    # 원격 임베딩 왕복은 한 번(실측 ~300ms), 두 번째 호출은 캐시에서 같은 벡터를 받는다.
    assert embedder.calls == [["배포 일정"]]
    assert store.embeddings == [[0.1, 0.2], [0.1, 0.2]]


async def test_vector_search_reembeds_when_the_model_differs(monkeypatch):
    """같은 문자열이라도 `embedding_model`이 다르면 캐시가 적중하면 안 된다."""
    embedder = _FakeEmbedder([0.1, 0.2])
    store = _FakeStore()
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: embedder)

    await vector_search("배포 일정", store, embedding_model="model-a")
    await vector_search("배포 일정", store, embedding_model="model-b")

    assert embedder.calls == [["배포 일정"], ["배포 일정"]]
    assert store.models == ["model-a", "model-b"]
    assert len(query_embedding_cache) == 2
