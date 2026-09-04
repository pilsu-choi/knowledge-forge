import pytest

from knowledge_forge.agent.plan import PlanState
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message
from knowledge_forge.agent.tools import default_tools
from knowledge_forge.config import settings

_EXPECTED_BASE_TOOL_NAMES = [
    "file_search",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "write_memory",
    "write_plan",
]


@pytest.fixture(autouse=True)
def _no_database_url(monkeypatch):
    """`default_tools()` now adds `vector_search` whenever
    `settings.database_url` is truthy - force it to None by default so this
    file's tool-list assertions don't depend on whether a real `.env` (e.g. a
    developer's local `KF_DATABASE_URL`) happens to be loaded. Tests that
    actually exercise `vector_search` override it back with monkeypatch."""
    monkeypatch.setattr(settings, "database_url", None)


async def test_default_tools_wraps_file_search_and_forwards_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured.update(query=query, project_id=project_id, type=type, limit=limit, since=since, until=until)
        return [{"path": "p"}]

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tools = default_tools()
    assert [t.name for t in tools] == _EXPECTED_BASE_TOOL_NAMES

    spec = next(t for t in tools if t.name == "file_search")
    result = await spec.handler({"query": "auth", "project_id": "proj", "type": "decision", "limit": 5})

    assert result == [{"path": "p"}]
    assert captured == {
        "query": "auth",
        "project_id": "proj",
        "type": "decision",
        "limit": 5,
        "since": None,
        "until": None,
    }


async def test_default_tools_handler_uses_defaults_for_optional_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured.update(query=query, project_id=project_id, type=type, limit=limit, since=since, until=until)
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    file_search_tool = next(t for t in default_tools() if t.name == "file_search")
    await file_search_tool.handler({"query": "auth"})

    assert captured == {"query": "auth", "project_id": None, "type": None, "limit": 20, "since": None, "until": None}


async def test_default_tools_file_search_clamps_an_oversized_limit(monkeypatch):
    """A model (or a prompt injection embedded in indexed content) asking for
    an enormous `limit` must not reach `search.service.file_search` as-is -
    `agent/tools.py::_MAX_SEARCH_LIMIT` caps it server-side."""
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured["limit"] = limit
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    file_search_tool = next(t for t in default_tools() if t.name == "file_search")
    await file_search_tool.handler({"query": "auth", "limit": 999_999})

    from knowledge_forge.agent.tools import _MAX_SEARCH_LIMIT

    assert captured["limit"] == _MAX_SEARCH_LIMIT


async def test_default_tools_write_plan_uses_a_fresh_state_when_none_is_given():
    tools = default_tools()
    write_plan = next(t for t in tools if t.name == "write_plan")

    result = await write_plan.handler({"steps": [{"content": "step 1", "status": "pending"}]})

    assert "[ ] step 1" in result


async def test_default_tools_write_plan_mutates_the_given_plan_state():
    plan_state = PlanState()
    tools = default_tools(plan_state=plan_state)
    write_plan = next(t for t in tools if t.name == "write_plan")

    await write_plan.handler({"steps": [{"content": "step 1", "status": "in_progress"}]})

    assert [s.content for s in plan_state.steps] == ["step 1"]
    assert plan_state.steps[0].status == "in_progress"


async def test_default_tools_read_file_forwards_to_fs_service(monkeypatch):
    captured = {}

    async def _fake_read_file(path):
        captured["path"] = path
        return {"path": path, "content": "hi"}

    monkeypatch.setattr("knowledge_forge.agent.tools.read_file", _fake_read_file)

    tool = next(t for t in default_tools() if t.name == "read_file")
    result = await tool.handler({"path": "default/note.md"})

    assert captured == {"path": "default/note.md"}
    assert result == {"path": "default/note.md", "content": "hi"}


async def test_default_tools_write_file_forwards_to_fs_service(monkeypatch):
    captured = {}

    async def _fake_write_file(path, content):
        captured.update(path=path, content=content)
        return {"path": path, "bytes_written": len(content)}

    monkeypatch.setattr("knowledge_forge.agent.tools.write_file", _fake_write_file)

    tool = next(t for t in default_tools() if t.name == "write_file")
    await tool.handler({"path": "default/new.md", "content": "hello"})

    assert captured == {"path": "default/new.md", "content": "hello"}


async def test_default_tools_edit_file_forwards_to_fs_service(monkeypatch):
    captured = {}

    async def _fake_edit_file(path, old_text, new_text):
        captured.update(path=path, old_text=old_text, new_text=new_text)
        return {"path": path, "replaced": True}

    monkeypatch.setattr("knowledge_forge.agent.tools.edit_file", _fake_edit_file)

    tool = next(t for t in default_tools() if t.name == "edit_file")
    await tool.handler({"path": "default/note.md", "old_text": "a", "new_text": "b"})

    assert captured == {"path": "default/note.md", "old_text": "a", "new_text": "b"}


async def test_default_tools_list_dir_defaults_to_empty_path(monkeypatch):
    captured = {}

    async def _fake_list_dir(path):
        captured["path"] = path
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.list_dir", _fake_list_dir)

    tool = next(t for t in default_tools() if t.name == "list_dir")
    await tool.handler({})

    assert captured == {"path": ""}


async def test_default_tools_write_memory_is_scoped_to_project_id(monkeypatch):
    captured = {}

    def _fake_write_memory(content, project_id=None, mode="append"):
        captured.update(content=content, project_id=project_id, mode=mode)
        return "resulting memory"

    monkeypatch.setattr("knowledge_forge.agent.tools.write_memory", _fake_write_memory)

    tool = next(t for t in default_tools(project_id="proj-a") if t.name == "write_memory")
    result = await tool.handler({"content": "fact"})

    assert captured == {"content": "fact", "project_id": "proj-a", "mode": "append"}
    assert result == "resulting memory"


async def test_default_tools_write_memory_respects_explicit_mode(monkeypatch):
    captured = {}

    def _fake_write_memory(content, project_id=None, mode="append"):
        captured.update(mode=mode)
        return ""

    monkeypatch.setattr("knowledge_forge.agent.tools.write_memory", _fake_write_memory)

    tool = next(t for t in default_tools() if t.name == "write_memory")
    await tool.handler({"content": "fact", "mode": "replace"})

    assert captured == {"mode": "replace"}


async def test_default_tools_omits_delegate_task_without_provider_and_system():
    tools = default_tools()

    assert "delegate_task" not in [t.name for t in tools]


class _FakeProvider(ChatProvider):
    async def send(self, messages, tools, system) -> AssistantTurn:
        raise AssertionError("should not be called in this test")


async def test_default_tools_includes_delegate_task_when_provider_and_system_given():
    tools = default_tools(provider=_FakeProvider(), system="system prompt")

    assert [t.name for t in tools] == ["file_search", "plan_search"] + _EXPECTED_BASE_TOOL_NAMES[1:] + [
        "delegate_task"
    ]


async def test_default_tools_include_delegate_false_omits_it_even_with_provider():
    tools = default_tools(provider=_FakeProvider(), system="system prompt", include_delegate=False)

    assert "delegate_task" not in [t.name for t in tools]


async def test_delegate_task_runs_a_child_agent_and_returns_only_its_final_answer():
    class _RecordingProvider(ChatProvider):
        def __init__(self):
            self.calls: list[list[Message]] = []

        async def send(self, messages, tools, system) -> AssistantTurn:
            self.calls.append(list(messages))
            assert "delegate_task" not in [t.name for t in tools]
            return AssistantTurn(text="child's final answer", tool_calls=[], stop_reason="end_turn")

    provider = _RecordingProvider()
    tools = default_tools(provider=provider, system="parent system prompt")
    delegate = next(t for t in tools if t.name == "delegate_task")

    result = await delegate.handler({"task": "investigate project X"})

    assert result == "child's final answer"
    # The child starts a fresh conversation - only its own task message, none
    # of the parent's history.
    assert len(provider.calls[0]) == 1
    assert provider.calls[0][0].content == "investigate project X"


async def test_default_tools_omits_vector_search_without_database_url():
    tools = default_tools()

    assert [t.name for t in tools] == _EXPECTED_BASE_TOOL_NAMES


async def test_default_tools_includes_vector_search_and_sql_search_right_after_file_search_when_database_url_set(
    monkeypatch,
):
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    tools = default_tools()

    assert [t.name for t in tools] == ["file_search", "vector_search", "sql_search"] + _EXPECTED_BASE_TOOL_NAMES[1:]


class _FakeConnectedStore:
    def __init__(self, sentinel):
        self._sentinel = sentinel

    async def __aenter__(self):
        return self._sentinel

    async def __aexit__(self, *exc_info):
        return False


async def test_default_tools_vector_search_opens_a_store_and_forwards_arguments(monkeypatch):
    captured = {}
    sentinel_store = object()

    def _fake_pipeline_store(database_url, data_dir):
        captured["database_url"] = database_url
        captured["data_dir"] = data_dir
        return _FakeConnectedStore(sentinel_store)

    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, since=None, until=None):
        captured.update(query=query, store=store, project_id=project_id, type=type, limit=limit, since=since, until=until)
        return [{"knowledge_object_id": "ko1"}]

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.vector_search", _fake_vector_search)

    tool = next(t for t in default_tools() if t.name == "vector_search")
    result = await tool.handler({"query": "배포 일정", "project_id": "proj", "type": "decision", "limit": 3})

    assert result == [{"knowledge_object_id": "ko1"}]
    assert captured["database_url"] == "postgresql://kf:kf@localhost/kf"
    assert captured["store"] is sentinel_store
    assert captured["query"] == "배포 일정"
    assert captured["project_id"] == "proj"
    assert captured["type"] == "decision"
    assert captured["limit"] == 3
    assert captured["since"] is None
    assert captured["until"] is None


async def test_default_tools_vector_search_clamps_an_oversized_limit(monkeypatch):
    captured = {}

    def _fake_pipeline_store(database_url, data_dir):
        return _FakeConnectedStore(object())

    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, since=None, until=None):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.vector_search", _fake_vector_search)

    tool = next(t for t in default_tools() if t.name == "vector_search")
    await tool.handler({"query": "배포 일정", "limit": 999_999})

    from knowledge_forge.agent.tools import _MAX_SEARCH_LIMIT

    assert captured["limit"] == _MAX_SEARCH_LIMIT


async def test_default_tools_vector_search_handler_uses_defaults_for_optional_arguments(monkeypatch):
    captured = {}

    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, since=None, until=None):
        captured.update(query=query, project_id=project_id, type=type, limit=limit, since=since, until=until)
        return []

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr(
        "knowledge_forge.storage.store.PipelineStore", lambda *a, **k: _FakeConnectedStore(object())
    )
    monkeypatch.setattr("knowledge_forge.agent.tools.vector_search", _fake_vector_search)

    tool = next(t for t in default_tools() if t.name == "vector_search")
    await tool.handler({"query": "auth"})

    assert captured == {"query": "auth", "project_id": None, "type": None, "limit": 10, "since": None, "until": None}


async def test_default_tools_omits_plan_search_without_provider():
    tools = default_tools()

    assert "plan_search" not in [t.name for t in tools]


async def test_default_tools_includes_plan_search_right_after_file_search_when_provider_given():
    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == ["file_search", "plan_search"] + _EXPECTED_BASE_TOOL_NAMES[1:]


async def test_default_tools_orders_vector_search_and_sql_search_before_plan_search(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == [
        "file_search",
        "vector_search",
        "sql_search",
        "plan_search",
    ] + _EXPECTED_BASE_TOOL_NAMES[1:]


async def test_default_tools_plan_search_clamps_an_oversized_limit_per_subquery(monkeypatch):
    captured = {}

    async def _fake_plan_and_search(query, given_provider, *, store=None, project_id=None, limit_per_subquery=5):
        captured["limit_per_subquery"] = limit_per_subquery
        return {"subqueries": [], "evidence": []}

    monkeypatch.setattr("knowledge_forge.agent.tools.plan_and_search", _fake_plan_and_search)

    tool = next(t for t in default_tools(provider=_FakeProvider()) if t.name == "plan_search")
    await tool.handler({"query": "복합 질문", "limit_per_subquery": 999_999})

    from knowledge_forge.agent.tools import _MAX_PLAN_SEARCH_LIMIT_PER_SUBQUERY

    assert captured["limit_per_subquery"] == _MAX_PLAN_SEARCH_LIMIT_PER_SUBQUERY


async def test_default_tools_plan_search_forwards_arguments_without_database_url(monkeypatch):
    captured = {}
    provider = _FakeProvider()

    async def _fake_plan_and_search(query, given_provider, *, store=None, project_id=None, limit_per_subquery=5):
        captured.update(
            query=query, provider=given_provider, store=store, project_id=project_id, limit_per_subquery=limit_per_subquery
        )
        return {"subqueries": [], "evidence": []}

    monkeypatch.setattr("knowledge_forge.agent.tools.plan_and_search", _fake_plan_and_search)

    tool = next(t for t in default_tools(project_id="proj-a", provider=provider) if t.name == "plan_search")
    result = await tool.handler({"query": "복합 질문", "limit_per_subquery": 3})

    assert result == {"subqueries": [], "evidence": []}
    assert captured == {
        "query": "복합 질문",
        "provider": provider,
        "store": None,
        "project_id": "proj-a",
        "limit_per_subquery": 3,
    }


async def test_default_tools_plan_search_opens_a_store_when_database_url_configured(monkeypatch):
    captured = {}
    sentinel_store = object()

    def _fake_pipeline_store(database_url, data_dir):
        captured["database_url"] = database_url
        return _FakeConnectedStore(sentinel_store)

    async def _fake_plan_and_search(query, provider, *, store=None, project_id=None, limit_per_subquery=5):
        captured["store"] = store
        return {"subqueries": [], "evidence": []}

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.plan_and_search", _fake_plan_and_search)

    tool = next(t for t in default_tools(provider=_FakeProvider()) if t.name == "plan_search")
    await tool.handler({"query": "복합 질문"})

    assert captured["database_url"] == "postgresql://kf:kf@localhost/kf"
    assert captured["store"] is sentinel_store


async def test_default_tools_plan_search_query_project_id_overrides_default(monkeypatch):
    captured = {}

    async def _fake_plan_and_search(query, provider, *, store=None, project_id=None, limit_per_subquery=5):
        captured["project_id"] = project_id
        return {"subqueries": [], "evidence": []}

    monkeypatch.setattr("knowledge_forge.agent.tools.plan_and_search", _fake_plan_and_search)

    tool = next(t for t in default_tools(project_id="default-proj", provider=_FakeProvider()) if t.name == "plan_search")
    await tool.handler({"query": "q", "project_id": "override-proj"})

    assert captured["project_id"] == "override-proj"


async def test_default_tools_file_search_resolves_a_natural_language_time_range(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured.update(since=since, until=until)
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tool = next(t for t in default_tools() if t.name == "file_search")
    await tool.handler({"query": "auth", "time_range": "어제"})

    assert captured["since"] is not None
    assert captured["until"] is not None
    assert (captured["until"] - captured["since"]).days == 1


async def test_default_tools_file_search_ignores_an_unrecognized_time_range(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured.update(since=since, until=until)
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tool = next(t for t in default_tools() if t.name == "file_search")
    await tool.handler({"query": "auth", "time_range": "이해할 수 없는 표현"})

    assert captured == {"since": None, "until": None}


async def test_default_tools_vector_search_resolves_a_natural_language_time_range(monkeypatch):
    captured = {}

    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, since=None, until=None):
        captured.update(since=since, until=until)
        return []

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr(
        "knowledge_forge.storage.store.PipelineStore", lambda *a, **k: _FakeConnectedStore(object())
    )
    monkeypatch.setattr("knowledge_forge.agent.tools.vector_search", _fake_vector_search)

    tool = next(t for t in default_tools() if t.name == "vector_search")
    await tool.handler({"query": "auth", "time_range": "지난주"})

    assert captured["since"] is not None
    assert captured["until"] is not None


async def test_default_tools_omits_sql_search_without_database_url():
    tools = default_tools()

    assert "sql_search" not in [t.name for t in tools]


async def test_default_tools_sql_search_description_embeds_the_schema(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    tool = next(t for t in default_tools() if t.name == "sql_search")

    assert "knowledge_objects" in tool.description
    assert "relations" in tool.description
    assert "SELECT" in tool.description


async def test_default_tools_sql_search_opens_a_store_and_forwards_the_query(monkeypatch):
    captured = {}
    sentinel_store = object()

    def _fake_pipeline_store(database_url, data_dir):
        captured["database_url"] = database_url
        return _FakeConnectedStore(sentinel_store)

    async def _fake_sql_search(sql, store, limit=200):
        captured.update(sql=sql, store=store)
        return {"columns": ["x"], "rows": [{"x": 1}], "row_count": 1, "truncated": False}

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.sql_search", _fake_sql_search)

    tool = next(t for t in default_tools() if t.name == "sql_search")
    result = await tool.handler({"sql": "SELECT 1 AS x"})

    assert result == {"columns": ["x"], "rows": [{"x": 1}], "row_count": 1, "truncated": False}
    assert captured["database_url"] == "postgresql://kf:kf@localhost/kf"
    assert captured["store"] is sentinel_store
    assert captured["sql"] == "SELECT 1 AS x"
