"""KF_EMBEDDING_PASSAGE_PREFIX / KF_EMBEDDING_QUERY_PREFIX는 provider에 보내는 텍스트에만 붙고
저장되는 embedded_text는 원문이다."""

from knowledge_forge.config import settings
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.storage.store import PipelineStore


class _Embedder:
    def __init__(self):
        self.sent: list[str] = []

    async def embed(self, texts):
        self.sent = list(texts)
        return [[0.0, 1.0] for _ in texts]


class _Db:
    records: list = []

    async def save_embeddings(self, records):
        self.records = records


async def test_passage_prefix_is_sent_but_not_stored(monkeypatch):
    monkeypatch.setattr(settings, "embedding_passage_prefix", "passage: ")
    store = PipelineStore.__new__(PipelineStore)
    store._db = _Db()
    chunk = Chunk(
        chunk_id="c1", document_id="d1", source_id="s1", hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=0, text="안녕  하세요", token_count=1, element_ids=["e1"],
    )
    embedder = _Embedder()

    await store.record_chunk_embeddings([chunk], "p", embedder, "m")

    assert embedder.sent == ["passage: 안녕 하세요"]
    assert store._db.records[0].embedded_text == "안녕 하세요"


async def test_query_prefix_is_sent_to_the_embedder(monkeypatch):
    from knowledge_forge.embedding.cache import query_embedding_cache
    from knowledge_forge.search import service

    embedder = _Embedder()
    monkeypatch.setattr(settings, "embedding_query_prefix", "Query: ")
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: embedder)
    query_embedding_cache._entries.clear()

    class _Store:
        async def search_by_embedding(self, *a, **k):
            return []

    await service.vector_search("질문", _Store(), embedding_model="prefix-test-model")

    assert embedder.sent == ["Query: 질문"]
