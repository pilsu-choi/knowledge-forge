from knowledge_forge.search.service import vector_search


class _FakeEmbedder:
    def __init__(self, vector):
        self._vector = vector
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [self._vector for _ in texts]


class _FakeStore:
    def __init__(self, hits):
        self._hits = hits
        self.calls: list[tuple] = []

    async def search_by_embedding(
        self,
        embedding,
        embedding_model,
        project_id=None,
        type=None,
        limit=10,
        since=None,
        until=None,
        target_kind=None,
        document_ids=None,
    ):
        self.calls.append((embedding, embedding_model, project_id, type, limit, since, until))
        self.target_kinds = getattr(self, "target_kinds", []) + [target_kind]
        return self._hits


async def test_vector_search_embeds_query_and_forwards_to_store(monkeypatch):
    embedder = _FakeEmbedder([0.1, 0.2, 0.3])
    store = _FakeStore([{"knowledge_object_id": "ko1", "score": 0.9}])
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: embedder)

    result = await vector_search("배포 결정", store, project_id="proj", type="decision", limit=5)

    assert result == [{"knowledge_object_id": "ko1", "score": 0.9}]
    assert embedder.calls == [["배포 결정"]]
    assert store.calls == [([0.1, 0.2, 0.3], "text-embedding-3-small", "proj", "decision", 5, None, None)]


async def test_vector_search_returns_empty_list_for_blank_query():
    store = _FakeStore([{"should": "not be returned"}])

    result = await vector_search("   ", store)

    assert result == []
    assert store.calls == []


async def test_vector_search_uses_default_limit_and_no_filters(monkeypatch):
    embedder = _FakeEmbedder([0.5])
    store = _FakeStore([])
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: embedder)

    await vector_search("q", store)

    assert store.calls == [([0.5], "text-embedding-3-small", None, None, 10, None, None)]


async def test_vector_search_forwards_since_and_until(monkeypatch):
    from datetime import datetime, timezone

    embedder = _FakeEmbedder([0.5])
    store = _FakeStore([])
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: embedder)
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 1, tzinfo=timezone.utc)

    await vector_search("q", store, since=since, until=until)

    assert store.calls[0][5:] == (since, until)


async def test_vector_search_uses_explicit_embedding_model(monkeypatch):
    embedder = _FakeEmbedder([0.5])
    captured = {}

    def _fake_get_embedding_provider(model=None):
        captured["model"] = model
        return embedder

    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", _fake_get_embedding_provider)
    store = _FakeStore([])

    await vector_search("q", store, embedding_model="custom-model")

    assert captured["model"] == "custom-model"
    assert store.calls[0][1] == "custom-model"


async def test_vector_search_forwards_target_kind_and_defaults_to_both_grains(monkeypatch):
    """`target_kind=None` (the agent tool's default) lets KO-grain and
    chunk-grain rows compete in one ranking; an explicit grain (what the
    evaluation golden set passes to measure one mode in isolation) must
    reach the store untouched."""
    embedder = _FakeEmbedder([0.5])
    store = _FakeStore([])
    monkeypatch.setattr("knowledge_forge.embedding.factory.get_embedding_provider", lambda model=None: embedder)

    await vector_search("q", store)
    await vector_search("q", store, target_kind="chunk")
    await vector_search("q", store, target_kind="knowledge_object")

    assert store.target_kinds == [None, "chunk", "knowledge_object"]
