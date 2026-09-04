import json

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider
from knowledge_forge.search.planner import (
    SubQuery,
    _parse_subqueries,
    _topological_waves,
    decompose_query,
    plan_and_search,
)


class _ScriptedProvider(ChatProvider):
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[str] = []

    async def send(self, messages, tools, system) -> AssistantTurn:
        self.calls.append(messages[0].content)
        return AssistantTurn(text=self._response_text, tool_calls=[], stop_reason="end_turn")


class _RaisingProvider(ChatProvider):
    async def send(self, messages, tools, system) -> AssistantTurn:
        raise RuntimeError("boom")


class _FakeStore:
    def __init__(self, hits_by_query: dict[str, list[dict]] | None = None):
        self._hits_by_query = hits_by_query or {}
        self.calls: list[tuple] = []

    async def search_by_embedding(self, embedding, embedding_model, project_id=None, type=None, limit=10):
        raise AssertionError("vector_search should call the embedder, not this directly")


# --- _parse_subqueries -------------------------------------------------


def test_parse_subqueries_returns_the_original_query_on_invalid_json():
    result = _parse_subqueries("not json at all", "원래 질문", max_subqueries=5)

    assert result == [SubQuery(id=1, query="원래 질문", depends_on=[])]


def test_parse_subqueries_returns_the_original_query_on_empty_list():
    result = _parse_subqueries("[]", "원래 질문", max_subqueries=5)

    assert result == [SubQuery(id=1, query="원래 질문", depends_on=[])]


def test_parse_subqueries_strips_markdown_code_fences():
    raw = '```json\n[{"query": "Q1", "depends_on": []}]\n```'

    result = _parse_subqueries(raw, "원래 질문", max_subqueries=5)

    assert result == [SubQuery(id=1, query="Q1", depends_on=[])]


def test_parse_subqueries_assigns_sequential_ids_and_keeps_valid_dependencies():
    raw = json.dumps(
        [
            {"query": "Q1", "depends_on": []},
            {"query": "Q2", "depends_on": [1]},
            {"query": "Q3", "depends_on": [1, 2]},
        ]
    )

    result = _parse_subqueries(raw, "원래 질문", max_subqueries=5)

    assert result == [
        SubQuery(id=1, query="Q1", depends_on=[]),
        SubQuery(id=2, query="Q2", depends_on=[1]),
        SubQuery(id=3, query="Q3", depends_on=[1, 2]),
    ]


def test_parse_subqueries_drops_forward_and_self_references():
    raw = json.dumps(
        [
            {"query": "Q1", "depends_on": [1, 2]},  # self + forward ref, both invalid at position 1
            {"query": "Q2", "depends_on": [1, 99]},  # 99 is out of range, dropped
        ]
    )

    result = _parse_subqueries(raw, "원래 질문", max_subqueries=5)

    assert result == [
        SubQuery(id=1, query="Q1", depends_on=[]),
        SubQuery(id=2, query="Q2", depends_on=[1]),
    ]


def test_parse_subqueries_truncates_to_max_subqueries():
    raw = json.dumps([{"query": f"Q{i}", "depends_on": []} for i in range(10)])

    result = _parse_subqueries(raw, "원래 질문", max_subqueries=3)

    assert [sq.query for sq in result] == ["Q0", "Q1", "Q2"]


def test_parse_subqueries_skips_malformed_items():
    raw = json.dumps([{"query": "Q1"}, {"not_a_query": True}, {"query": "   "}, {"query": "Q2", "depends_on": []}])

    result = _parse_subqueries(raw, "원래 질문", max_subqueries=5)

    assert [sq.query for sq in result] == ["Q1", "Q2"]


# --- decompose_query -----------------------------------------------------


async def test_decompose_query_parses_the_provider_response():
    provider = _ScriptedProvider(json.dumps([{"query": "Q1", "depends_on": []}, {"query": "Q2", "depends_on": [1]}]))

    result = await decompose_query("복합 질문", provider, max_subqueries=5)

    assert result == [
        SubQuery(id=1, query="Q1", depends_on=[]),
        SubQuery(id=2, query="Q2", depends_on=[1]),
    ]
    assert provider.calls == ["복합 질문"]


async def test_decompose_query_falls_back_to_the_original_query_when_the_provider_fails():
    provider = _RaisingProvider()

    result = await decompose_query("단순 질문", provider, max_subqueries=5)

    assert result == [SubQuery(id=1, query="단순 질문", depends_on=[])]


# --- _topological_waves ---------------------------------------------------


def test_topological_waves_groups_independent_subqueries_into_one_wave():
    subqueries = [
        SubQuery(id=1, query="Q1", depends_on=[]),
        SubQuery(id=2, query="Q2", depends_on=[]),
        SubQuery(id=3, query="Q3", depends_on=[]),
    ]

    waves = _topological_waves(subqueries)

    assert len(waves) == 1
    assert {sq.id for sq in waves[0]} == {1, 2, 3}


def test_topological_waves_sequences_dependent_subqueries_into_separate_waves():
    subqueries = [
        SubQuery(id=1, query="Q1", depends_on=[]),
        SubQuery(id=2, query="Q2", depends_on=[1]),
        SubQuery(id=3, query="Q3", depends_on=[2]),
    ]

    waves = _topological_waves(subqueries)

    assert [[sq.id for sq in wave] for wave in waves] == [[1], [2], [3]]


def test_topological_waves_mixes_parallel_and_sequential_correctly():
    # Q1, Q2 independent (wave 1); Q3 depends on both (wave 2); Q4 independent (wave 1 too).
    subqueries = [
        SubQuery(id=1, query="Q1", depends_on=[]),
        SubQuery(id=2, query="Q2", depends_on=[]),
        SubQuery(id=3, query="Q3", depends_on=[1, 2]),
        SubQuery(id=4, query="Q4", depends_on=[]),
    ]

    waves = _topological_waves(subqueries)

    assert [{sq.id for sq in wave} for wave in waves] == [{1, 2, 4}, {3}]


# --- plan_and_search (end to end, with fakes) ------------------------------


async def test_plan_and_search_executes_independent_subqueries_and_collects_evidence(monkeypatch):
    provider = _ScriptedProvider(
        json.dumps([{"query": "지난달 AI 프로젝트 목록", "depends_on": []}, {"query": "각 프로젝트 성과", "depends_on": []}])
    )

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        return [{"path": f"p-{query}", "title": query, "snippet": f"snippet for {query}"}]

    monkeypatch.setattr("knowledge_forge.search.planner.file_search", _fake_file_search)

    result = await plan_and_search("지난달 성과 좋은 AI 프로젝트는?", provider, store=None)

    assert [sq["query"] for sq in result["subqueries"]] == ["지난달 AI 프로젝트 목록", "각 프로젝트 성과"]
    assert len(result["evidence"]) == 2
    for item in result["evidence"]:
        assert item["results"][0]["retrieval_sources"] == ["file_search"]
        assert item["results"][0]["path"] == f"p-{item['subquery']}"

    # The first sub-query's own text contains "지난달" - it should carry a
    # resolved time_range; the second has no temporal expression at all.
    assert result["evidence"][0]["time_range"]["label"] == "지난달"
    assert result["evidence"][1]["time_range"] is None


async def test_plan_and_search_augments_dependent_subquery_with_prior_results(monkeypatch):
    provider = _ScriptedProvider(
        json.dumps(
            [
                {"query": "가장 성과가 좋은 프로젝트", "depends_on": []},
                {"query": "그 프로젝트의 성공 이유", "depends_on": [1]},
            ]
        )
    )
    captured_queries: list[str] = []

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured_queries.append(query)
        if query == "가장 성과가 좋은 프로젝트":
            return [{"path": "p1", "summary": "Project Phoenix가 가장 성과가 좋았다"}]
        return [{"path": "p2", "summary": "이유 상세"}]

    monkeypatch.setattr("knowledge_forge.search.planner.file_search", _fake_file_search)

    result = await plan_and_search("가장 성과가 좋은 프로젝트와 그 이유는?", provider, store=None)

    assert captured_queries[0] == "가장 성과가 좋은 프로젝트"
    assert "그 프로젝트의 성공 이유" in captured_queries[1]
    assert "Project Phoenix가 가장 성과가 좋았다" in captured_queries[1]
    assert len(result["evidence"]) == 2


async def test_plan_and_search_excludes_vector_search_when_no_store_is_given(monkeypatch):
    provider = _ScriptedProvider(json.dumps([{"query": "단순 질문", "depends_on": []}]))
    vector_search_called = False

    async def _fake_vector_search(*args, **kwargs):
        nonlocal vector_search_called
        vector_search_called = True
        return []

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        return [{"path": "p1"}]

    monkeypatch.setattr("knowledge_forge.search.planner.file_search", _fake_file_search)
    monkeypatch.setattr("knowledge_forge.search.planner.vector_search", _fake_vector_search)

    await plan_and_search("단순 질문", provider, store=None)

    assert vector_search_called is False


async def test_plan_and_search_uses_vector_search_when_store_is_given(monkeypatch):
    # A long natural-language question routes to vector_search (+ file_search).
    provider = _ScriptedProvider(
        json.dumps([{"query": "김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", "depends_on": []}])
    )

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        return [{"path": "p1"}]

    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, since=None, until=None):
        return [{"knowledge_object_id": "ko1", "score": 0.9}]

    monkeypatch.setattr("knowledge_forge.search.planner.file_search", _fake_file_search)
    monkeypatch.setattr("knowledge_forge.search.planner.vector_search", _fake_vector_search)

    store = _FakeStore()
    result = await plan_and_search(
        "김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", provider, store=store
    )

    sources: set[str] = set()
    for hit in result["evidence"][0]["results"]:
        sources.update(hit["retrieval_sources"])
    assert sources == {"file_search", "vector_search"}


async def test_plan_and_search_deduplicates_a_knowledge_object_both_sources_find(monkeypatch):
    provider = _ScriptedProvider(
        json.dumps([{"query": "김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", "depends_on": []}])
    )

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        return [{"path": "objects/decision/p1.md", "title": "실패 원인", "knowledge_object_id": "ko-shared"}]

    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, since=None, until=None):
        return [{"knowledge_object_id": "ko-shared", "score": 0.9}]

    monkeypatch.setattr("knowledge_forge.search.planner.file_search", _fake_file_search)
    monkeypatch.setattr("knowledge_forge.search.planner.vector_search", _fake_vector_search)

    result = await plan_and_search(
        "김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", provider, store=_FakeStore()
    )

    results = result["evidence"][0]["results"]
    assert len(results) == 1
    assert sorted(results[0]["retrieval_sources"]) == ["file_search", "vector_search"]
    assert results[0]["title"] == "실패 원인"
    assert results[0]["score"] == 0.9


async def test_plan_and_search_forwards_a_temporal_filter_detected_in_the_subquery(monkeypatch):
    provider = _ScriptedProvider(json.dumps([{"query": "어제 있었던 주요 결정", "depends_on": []}]))
    captured: dict = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured["since"] = since
        captured["until"] = until
        return []

    monkeypatch.setattr("knowledge_forge.search.planner.file_search", _fake_file_search)

    result = await plan_and_search("어제 뭐 결정했어?", provider, store=None)

    assert captured["since"] is not None
    assert captured["until"] is not None
    assert (captured["until"] - captured["since"]).days == 1
    assert result["evidence"][0]["time_range"]["label"] == "어제"
    assert result["evidence"][0]["time_range"]["since"] == captured["since"].isoformat()
    assert result["evidence"][0]["time_range"]["until"] == captured["until"].isoformat()


async def test_plan_and_search_time_range_is_none_without_a_temporal_expression(monkeypatch):
    provider = _ScriptedProvider(json.dumps([{"query": "인증 방식 관련 결정", "depends_on": []}]))
    captured: dict = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured["since"] = since
        captured["until"] = until
        return []

    monkeypatch.setattr("knowledge_forge.search.planner.file_search", _fake_file_search)

    result = await plan_and_search("인증 방식을 왜 바꿨어?", provider, store=None)

    assert captured["since"] is None
    assert captured["until"] is None
    assert result["evidence"][0]["time_range"] is None
