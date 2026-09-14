import os

import pytest

from knowledge_forge.agent.plan import PlanState
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message
from knowledge_forge.agent.tools import default_tools
from knowledge_forge.config import settings

# 검색 도구 표면(`KF_AGENT_TOOL_SURFACE`)과 무관하게 항상 같은, 검색이 아닌 도구들.
# 파일시스템 도구는 `KF_OKF_DIR`이 있을 때만 앞에 붙는다(`_FILESYSTEM_TOOL_NAMES`).
_EXPECTED_BASE_TOOL_NAMES = [
    "write_memory",
    "write_plan",
]
_FILESYSTEM_TOOL_NAMES = ["read_file", "write_file", "edit_file", "list_dir"]


@pytest.fixture(autouse=True)
def _no_database_url(monkeypatch):
    """`default_tools()` now adds `vector_search` whenever
    `settings.database_url` is truthy - force it to None by default so this
    file's tool-list assertions don't depend on whether a real `.env` (e.g. a
    developer's local `KF_DATABASE_URL`) happens to be loaded. Tests that
    actually exercise `vector_search` override it back with monkeypatch."""
    monkeypatch.setattr(settings, "database_url", None)


@pytest.fixture(autouse=True)
def _default_tool_surface(monkeypatch):
    """같은 이유로 `KF_AGENT_TOOL_SURFACE`도 기본값에 고정한다 - 개발자 로컬
    `.env`가 "full"이면 이 파일의 목록 단언이 전부 흔들린다. "full"을 실제로
    확인하는 테스트만 monkeypatch로 되돌린다."""
    monkeypatch.setattr(settings, "agent_tool_surface", "minimal")


@pytest.fixture(autouse=True)
def _file_search_off_by_default(monkeypatch):
    """`file_search`는 표면과 무관하게 `KF_AGENT_FILE_SEARCH_ENABLED`(기본 False)가
    끄고 있다 - 위 두 fixture와 같은 이유로 여기서도 문서화된 기본값에 고정한다.
    이 도구를 실제로 확인하는 테스트는 `_enable_file_search`로 되돌린다."""
    monkeypatch.setattr(settings, "agent_file_search_enabled", False)


@pytest.fixture(autouse=True)
def _no_okf_dir(monkeypatch):
    """파일시스템 도구는 `KF_OKF_DIR`이 있을 때만 실린다 - 개발자 로컬 `.env`에 그 값이
    있으면 목록 단언이 흔들리므로 없음에 고정한다. 도구를 실제로 확인하는 테스트는
    `_set_okf_dir`로 되돌린다."""
    monkeypatch.setattr(settings, "okf_dir", None)


def _set_okf_dir(monkeypatch):
    monkeypatch.setattr(settings, "okf_dir", "/tmp/okf")


def _full_surface(monkeypatch):
    monkeypatch.setattr(settings, "agent_tool_surface", "full")


def _enable_file_search(monkeypatch):
    monkeypatch.setattr(settings, "agent_file_search_enabled", True)


async def test_default_tools_wraps_file_search_and_forwards_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20, since=None, until=None):
        captured.update(query=query, project_id=project_id, type=type, limit=limit, since=since, until=until)
        return [{"path": "p"}]

    _enable_file_search(monkeypatch)
    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tools = default_tools()
    assert [t.name for t in tools] == ["file_search"] + _EXPECTED_BASE_TOOL_NAMES

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

    _enable_file_search(monkeypatch)
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

    _enable_file_search(monkeypatch)
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
    _set_okf_dir(monkeypatch)

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
    _set_okf_dir(monkeypatch)

    tool = next(t for t in default_tools() if t.name == "write_file")
    await tool.handler({"path": "default/new.md", "content": "hello"})

    assert captured == {"path": "default/new.md", "content": "hello"}


async def test_default_tools_edit_file_forwards_to_fs_service(monkeypatch):
    captured = {}

    async def _fake_edit_file(path, old_text, new_text):
        captured.update(path=path, old_text=old_text, new_text=new_text)
        return {"path": path, "replaced": True}

    monkeypatch.setattr("knowledge_forge.agent.tools.edit_file", _fake_edit_file)
    _set_okf_dir(monkeypatch)

    tool = next(t for t in default_tools() if t.name == "edit_file")
    await tool.handler({"path": "default/note.md", "old_text": "a", "new_text": "b"})

    assert captured == {"path": "default/note.md", "old_text": "a", "new_text": "b"}


async def test_default_tools_list_dir_defaults_to_empty_path(monkeypatch):
    captured = {}

    async def _fake_list_dir(path):
        captured["path"] = path
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.list_dir", _fake_list_dir)
    _set_okf_dir(monkeypatch)

    tool = next(t for t in default_tools() if t.name == "list_dir")
    await tool.handler({})

    assert captured == {"path": ""}


async def test_filesystem_tools_are_omitted_without_okf_dir(monkeypatch):
    """`KF_OKF_DIR`이 없으면 네 파일시스템 도구는 호출해도 fs_service가 전부 거부한다 -
    목록에 남으면 모델이 그걸 불러 턴을 버린다. DB가 있는 기본 배포(에이전트 평가 구성)에서
    빠지는지를 못 박는다."""
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    names = [t.name for t in default_tools(provider=_FakeProvider())]

    assert not set(_FILESYSTEM_TOOL_NAMES) & set(names)


async def test_filesystem_tools_are_included_when_okf_dir_is_set(monkeypatch):
    _set_okf_dir(monkeypatch)

    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == _FILESYSTEM_TOOL_NAMES + _EXPECTED_BASE_TOOL_NAMES


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

    assert [t.name for t in tools] == _EXPECTED_BASE_TOOL_NAMES + ["delegate_task"]


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


async def test_default_tools_default_surface_exposes_only_search_read_document_and_sql_search(monkeypatch):
    """**기본 표면 회귀 테스트.** `search/retrieve.py`가 "어떤 검색을 어떤 순서로"
    결정을 LLM에게서 빼앗아 결정론적 함수로 만든 모듈인데(그 docstring), 도구
    목록이 개별 검색 도구를 다시 늘어놓으면 그 결정이 모델에게 되돌아간다 -
    이 단언이 그 재발(도구 목록이 조용히 다시 자라는 것)을 막는 자리다.
    개별 도구를 되살리려면 `KF_AGENT_TOOL_SURFACE=full`을 써야 한다."""
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == ["search", "read_document", "sql_search"] + _EXPECTED_BASE_TOOL_NAMES


async def test_default_tools_default_surface_falls_back_to_file_search_without_database_url(monkeypatch):
    """Postgres 없는 OKF 전용 배포 - search/read_document/sql_search가 전부 빠지므로
    Postgres를 요구하지 않는 유일한 검색 경로인 file_search가 그 자리를 대신한다
    (`search/service.py` docstring의 "OKF-only deployment"). 단 `file_search` 자체가
    `KF_AGENT_FILE_SEARCH_ENABLED`로 꺼져 있으므로, 그 구성을 쓰려면 켜야 한다."""
    _enable_file_search(monkeypatch)

    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == ["file_search"] + _EXPECTED_BASE_TOOL_NAMES


async def test_file_search_is_off_by_default_even_where_it_used_to_be_the_only_search_tool():
    """`KF_AGENT_FILE_SEARCH_ENABLED` 기본 False의 회귀 테스트. OKF 전용 구성(위 테스트와
    같은 상황)에서도 빠진다 - 그래서 그 배포는 이 값을 켜야 하고, 그 사실을 여기서 못
    박아 둔다(config.py의 그 필드 주석에 왜 껐는지가 있다)."""
    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == _EXPECTED_BASE_TOOL_NAMES


async def test_default_tools_full_surface_restores_every_narrow_search_tool(monkeypatch):
    _full_surface(monkeypatch)
    _enable_file_search(monkeypatch)
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    tools = default_tools()

    assert [t.name for t in tools] == [
        "search",
        "file_search",
        "read_document",
        "sql_search",
        "vector_search",
        "document_search",
        "lexical_search",
    ] + _EXPECTED_BASE_TOOL_NAMES


class _FakeConnectedStore:
    def __init__(self, sentinel):
        self._sentinel = sentinel

    async def __aenter__(self):
        return self._sentinel

    async def __aexit__(self, *exc_info):
        return False


async def test_default_tools_vector_search_opens_a_store_and_forwards_arguments(monkeypatch):
    _full_surface(monkeypatch)
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

    # `project_id`는 스키마에서 빠졌다(결함8) - 세션 값(`default_tools(project_id=...)`)을
    # 항상 쓴다. args에 뭘 넣어도(여기선 애초에 스키마에 없으니 넣지도 않는다) 무시된다.
    tool = next(t for t in default_tools(project_id="session-proj") if t.name == "vector_search")
    result = await tool.handler({"query": "배포 일정", "type": "decision", "limit": 3})

    assert result == [{"knowledge_object_id": "ko1"}]
    assert captured["database_url"] == "postgresql://kf:kf@localhost/kf"
    assert captured["store"] is sentinel_store
    assert captured["query"] == "배포 일정"
    assert captured["project_id"] == "session-proj"
    assert captured["type"] == "decision"
    assert captured["limit"] == 3
    assert captured["since"] is None
    assert captured["until"] is None
    assert "project_id" not in tool.input_schema["properties"]


async def test_default_tools_vector_search_ignores_a_model_supplied_project_id(monkeypatch):
    """결함8(`wiki/search-code-review-2026-09-14.md`): 모델이 지어낸 project_id(관측:
    "NH_MVP", "project_123")가 세션 값을 덮어써 0건 응답을 냈다 - 스키마에서 빼고, 혹시
    args에 실려 와도(스키마에 없는 여분 필드를 모델이 그래도 넣는 경우) 무시한다."""
    _full_surface(monkeypatch)
    captured = {}

    def _fake_pipeline_store(database_url, data_dir):
        return _FakeConnectedStore(object())

    async def _fake_vector_search(query, store, project_id=None, type=None, limit=10, since=None, until=None):
        captured["project_id"] = project_id
        return []

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.vector_search", _fake_vector_search)

    tool = next(t for t in default_tools(project_id="session-proj") if t.name == "vector_search")
    await tool.handler({"query": "q", "project_id": "invented-by-model"})

    assert captured["project_id"] == "session-proj"


async def test_default_tools_document_search_opens_a_store_and_forwards_arguments(monkeypatch):
    _full_surface(monkeypatch)
    captured = {}
    sentinel_store = object()

    def _fake_pipeline_store(database_url, data_dir):
        captured["database_url"] = database_url
        return _FakeConnectedStore(sentinel_store)

    async def _fake_document_search(query, store, project_id=None, source_type=None, limit=10, since=None, until=None):
        captured.update(query=query, store=store, project_id=project_id, source_type=source_type, limit=limit, since=since, until=until)
        return [{"document_id": "d1", "target_kind": "document"}]

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.document_search", _fake_document_search)

    from knowledge_forge.agent.tools import _MAX_SEARCH_LIMIT

    # project_id는 스키마에 없다(결함8) - 세션 값을 쓴다.
    tool = next(t for t in default_tools(project_id="session-proj") if t.name == "document_search")
    result = await tool.handler({"query": "SH 보고서", "source_type": "google_drive", "limit": 999})

    assert result == [{"document_id": "d1", "target_kind": "document"}]
    assert "특정" in tool.description
    assert captured["store"] is sentinel_store
    assert captured["query"] == "SH 보고서"
    assert captured["project_id"] == "session-proj"
    assert captured["source_type"] == "google_drive"
    assert captured["limit"] == _MAX_SEARCH_LIMIT
    assert captured["since"] is None
    assert "project_id" not in tool.input_schema["properties"]


async def test_default_tools_lexical_search_opens_a_store_and_resolves_time_range(monkeypatch):
    _full_surface(monkeypatch)
    captured = {}
    sentinel_store = object()

    def _fake_pipeline_store(database_url, data_dir):
        return _FakeConnectedStore(sentinel_store)

    async def _fake_lexical_search(query, store, project_id=None, limit=10, since=None, until=None):
        captured.update(query=query, store=store, project_id=project_id, limit=limit, since=since, until=until)
        return [{"document_id": "d1", "chunk_id": "c1", "target_kind": "chunk"}]

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.lexical_search", _fake_lexical_search)

    tool = next(t for t in default_tools() if t.name == "lexical_search")
    result = await tool.handler({"query": "ERR_CONNECTION_RESET", "time_range": "지난주"})

    assert result == [{"document_id": "d1", "chunk_id": "c1", "target_kind": "chunk"}]
    assert captured["store"] is sentinel_store
    assert captured["query"] == "ERR_CONNECTION_RESET"
    assert captured["project_id"] is None
    assert captured["limit"] == 10
    assert captured["since"] is not None and captured["until"] is not None


async def test_default_tools_vector_search_clamps_an_oversized_limit(monkeypatch):
    _full_surface(monkeypatch)
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
    _full_surface(monkeypatch)
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


async def test_default_tools_omits_plan_search_without_provider(monkeypatch):
    _full_surface(monkeypatch)

    tools = default_tools()

    assert "plan_search" not in [t.name for t in tools]


async def test_default_tools_omits_plan_search_on_the_default_surface_even_with_provider(monkeypatch):
    """`plan_search`는 "full"에서만 나온다 - 멀티홉 분해 자체가 사라지는 건 아니고,
    `search`가 multihop 전략으로 같은 `plan_and_search()`를 안에서 부른다."""
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    tools = default_tools(provider=_FakeProvider())

    assert "plan_search" not in [t.name for t in tools]


async def test_default_tools_includes_plan_search_on_the_full_surface_when_provider_given(monkeypatch):
    _full_surface(monkeypatch)
    _enable_file_search(monkeypatch)

    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == ["file_search", "plan_search"] + _EXPECTED_BASE_TOOL_NAMES


async def test_default_tools_orders_plan_search_after_every_other_search_tool(monkeypatch):
    _full_surface(monkeypatch)
    _enable_file_search(monkeypatch)
    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")

    tools = default_tools(provider=_FakeProvider())

    assert [t.name for t in tools] == [
        "search",
        "file_search",
        "read_document",
        "sql_search",
        "vector_search",
        "document_search",
        "lexical_search",
        "plan_search",
    ] + _EXPECTED_BASE_TOOL_NAMES


async def test_default_tools_plan_search_clamps_an_oversized_limit_per_subquery(monkeypatch):
    _full_surface(monkeypatch)
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
    _full_surface(monkeypatch)
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
    _full_surface(monkeypatch)
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
    _full_surface(monkeypatch)
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

    _enable_file_search(monkeypatch)
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

    _enable_file_search(monkeypatch)
    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tool = next(t for t in default_tools() if t.name == "file_search")
    await tool.handler({"query": "auth", "time_range": "이해할 수 없는 표현"})

    assert captured == {"since": None, "until": None}


async def test_default_tools_vector_search_resolves_a_natural_language_time_range(monkeypatch):
    _full_surface(monkeypatch)
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


async def test_default_tools_read_document_opens_a_store_and_forwards_arguments(monkeypatch):
    captured = {}
    sentinel_store = object()

    def _fake_pipeline_store(database_url, data_dir):
        captured["database_url"] = database_url
        return _FakeConnectedStore(sentinel_store)

    async def _fake_read_document(document_id, store, *, max_chars=None, offset=0):
        captured.update(document_id=document_id, store=store, max_chars=max_chars, offset=offset)
        return {"document_id": document_id, "chunks": [], "truncated": False, "next_offset": None}

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.read_document", _fake_read_document)

    tool = next(t for t in default_tools() if t.name == "read_document")
    result = await tool.handler({"document_id": "doc-1", "offset": 7, "max_chars": 1234})

    assert result["document_id"] == "doc-1"
    assert captured["database_url"] == "postgresql://kf:kf@localhost/kf"
    assert captured["store"] is sentinel_store
    assert captured["document_id"] == "doc-1"
    assert captured["offset"] == 7
    assert captured["max_chars"] == 1234


async def test_default_tools_read_document_defaults_and_clamps_max_chars(monkeypatch):
    from knowledge_forge.agent.tools import _READ_DOCUMENT_DEFAULT_MAX_CHARS, _READ_DOCUMENT_MAX_CHARS

    captured = []

    def _fake_pipeline_store(database_url, data_dir):
        return _FakeConnectedStore(object())

    async def _fake_read_document(document_id, store, *, max_chars=None, offset=0):
        captured.append((max_chars, offset))
        return {"document_id": document_id}

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.read_document", _fake_read_document)

    tool = next(t for t in default_tools() if t.name == "read_document")
    await tool.handler({"document_id": "doc-1"})
    await tool.handler({"document_id": "doc-1", "max_chars": 999_999_999, "offset": -5})
    await tool.handler({"document_id": "doc-1", "max_chars": "not-a-number", "offset": "nope"})

    assert captured == [
        (_READ_DOCUMENT_DEFAULT_MAX_CHARS, 0),
        (_READ_DOCUMENT_MAX_CHARS, 0),
        (_READ_DOCUMENT_DEFAULT_MAX_CHARS, 0),
    ]


async def test_default_tools_read_document_raises_a_clear_error_for_an_unknown_document(monkeypatch):
    def _fake_pipeline_store(database_url, data_dir):
        return _FakeConnectedStore(object())

    async def _fake_read_document(document_id, store, *, max_chars=None, offset=0):
        return None

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.read_document", _fake_read_document)

    tool = next(t for t in default_tools() if t.name == "read_document")
    with pytest.raises(LookupError, match="document not found: doc-nope"):
        await tool.handler({"document_id": "doc-nope"})


async def test_default_tools_search_opens_a_store_forwards_provider_and_caps_hits(monkeypatch):
    """The unified `search` tool is a thin adapter over `search/retrieve.py::retrieve`
    - it must hand the session provider/project_id through, clamp `limit`, and
    return only the top-limit hits with each summary capped."""
    from knowledge_forge.search.retrieve import RetrievalResult, RetrievalStep

    captured = {}
    sentinel_store = object()
    provider = _FakeProvider()

    def _fake_pipeline_store(database_url, data_dir):
        captured["database_url"] = database_url
        return _FakeConnectedStore(sentinel_store)

    async def _fake_retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, **kwargs):
        captured.update(query=query, store=store, provider=provider, project_id=project_id, limit=limit)
        hits = [{"document_id": f"d{i}", "target_kind": "chunk", "score": 1.0, "summary": "x" * 1000} for i in range(60)]
        return RetrievalResult(
            query=query,
            constraints={},
            strategies=["vector"],
            steps=[RetrievalStep(strategy="vector", source="vector_search", n_hits=60, detail={})],
            hits=hits,
            document_ids=[h["document_id"] for h in hits],
            scores=[1.0] * 60,
            predicted_count=None,
            documents_read=[],
            elapsed_ms=1.0,
        )

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.retrieve", _fake_retrieve)

    from knowledge_forge.agent.tools import _MAX_SEARCH_LIMIT, _SEARCH_HIT_SUMMARY_CHARS

    tool = next(t for t in default_tools(project_id="proj-a", provider=provider) if t.name == "search")
    assert "통합 검색" in tool.description
    result = await tool.handler({"query": "지난주 회의록 요약해줘", "limit": 999_999})

    assert captured["store"] is sentinel_store
    assert captured["provider"] is provider
    assert captured["project_id"] == "proj-a"
    assert captured["limit"] == _MAX_SEARCH_LIMIT
    assert len(result["hits"]) == _MAX_SEARCH_LIMIT
    assert all(len(h["summary"]) == _SEARCH_HIT_SUMMARY_CHARS for h in result["hits"])
    assert result["steps"][0]["strategy"] == "vector"
    assert result["document_ids"][0] == "d0"


async def test_default_tools_search_does_not_truncate_a_wide_exhaustive_query(monkeypatch):
    """결함9(`wiki/search-code-review-2026-09-14.md`): `retrieve()`는 exhaustive/negative/
    count(wide) 질의에서 최대 200건(`EXHAUSTIVE_LIMIT`)을 돌려주는데, 도구가
    `document_ids[:limit]`/`hits[:limit]`으로 10~50건까지 다시 잘라 predicted_count와
    실제로 돌려준 목록 길이가 어긋났다. wide면 `document_ids`를 그대로 돌려주고,
    `hits`는 그 문서들에 속한 것만(청크 단위 인덱스 절단이 아니라 문서 단위로) 담는다."""
    from knowledge_forge.search.retrieve import RetrievalResult, RetrievalStep

    def _fake_pipeline_store(database_url, data_dir):
        return _FakeConnectedStore(object())

    async def _fake_retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, **kwargs):
        # 130개 문서, 문서마다 청크 히트 2개(청크 단위 인덱스 절단이면 65개 문서만 남는다).
        document_ids = [f"d{i}" for i in range(130)]
        hits = [
            {"document_id": did, "chunk_id": f"{did}-{j}", "target_kind": "chunk", "score": 1.0, "summary": "x"}
            for did in document_ids
            for j in range(2)
        ]
        return RetrievalResult(
            query=query,
            constraints={},
            strategies=["exhaustive"],
            steps=[RetrievalStep(strategy="exhaustive", source="metadata_filter", n_hits=len(hits), detail={})],
            hits=hits,
            document_ids=document_ids,
            scores=[1.0] * len(document_ids),
            predicted_count=130,
            documents_read=[],
            elapsed_ms=1.0,
        )

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.retrieve", _fake_retrieve)

    tool = next(t for t in default_tools() if t.name == "search")
    result = await tool.handler({"query": "관련 문서 전부", "limit": 10})

    assert len(result["document_ids"]) == 130
    assert result["predicted_count"] == 130
    assert result["predicted_count"] == len(result["document_ids"])
    hit_document_ids = {h["document_id"] for h in result["hits"]}
    assert hit_document_ids <= set(result["document_ids"])
    # 문서 단위로 잘랐다는 것 - 어떤 문서가 살아남았든 그 문서의 청크 히트 2개가 함께 있다
    # (인덱스로 잘랐다면 마지막에 살아남은 문서는 청크 하나만 담겼을 것이다).
    for did in hit_document_ids:
        assert sum(1 for h in result["hits"] if h["document_id"] == did) == 2


async def test_default_tools_search_narrow_query_still_truncates_to_the_limit(monkeypatch):
    """회귀 방지: wide가 아닌 일반 질의는 기존처럼 limit으로 잘려야 한다."""
    from knowledge_forge.search.retrieve import RetrievalResult

    def _fake_pipeline_store(database_url, data_dir):
        return _FakeConnectedStore(object())

    async def _fake_retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, **kwargs):
        document_ids = [f"d{i}" for i in range(30)][:limit]
        hits = [{"document_id": did, "target_kind": "document", "score": 1.0} for did in document_ids]
        return RetrievalResult(
            query=query,
            constraints={},
            strategies=["vector"],
            steps=[],
            hits=hits,
            document_ids=document_ids,
            scores=[1.0] * len(document_ids),
            predicted_count=None,
            documents_read=[],
            elapsed_ms=1.0,
        )

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.retrieve", _fake_retrieve)

    tool = next(t for t in default_tools() if t.name == "search")
    result = await tool.handler({"query": "인증 방식을 OAuth로 바꾼 결정", "limit": 10})

    assert len(result["document_ids"]) == 10


async def test_default_tools_share_one_lazily_opened_store_across_db_tool_calls(monkeypatch):
    """결함10: 예전엔 검색 도구 호출마다 `PipelineStore`(+ 그 안의 4-커넥션 읽기 풀)를 새로
    열고 닫았다 - 이제 도구 세트 하나(`default_tools()` 한 번 호출)가 첫 DB 도구 호출에서만
    열고, 이후 다른 DB 도구를 불러도 그 인스턴스를 그대로 재사용한다."""
    from knowledge_forge.search.retrieve import RetrievalResult

    opens: list[str] = []

    def _fake_pipeline_store(database_url, data_dir):
        opens.append(database_url)
        return _FakeConnectedStore(object())

    async def _fake_retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, **kwargs):
        return RetrievalResult(query, {}, [], [], [], [], [], None, [], 0.0)

    async def _fake_read_document(document_id, store, *, max_chars=None, offset=0):
        return {"document_id": document_id}

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", _fake_pipeline_store)
    monkeypatch.setattr("knowledge_forge.agent.tools.retrieve", _fake_retrieve)
    monkeypatch.setattr("knowledge_forge.agent.tools.read_document", _fake_read_document)

    tools = default_tools()
    search_tool = next(t for t in tools if t.name == "search")
    read_document_tool = next(t for t in tools if t.name == "read_document")

    await search_tool.handler({"query": "q1"})
    await read_document_tool.handler({"document_id": "d1"})
    await search_tool.handler({"query": "q2"})

    assert opens == ["postgresql://kf:kf@localhost/kf"]  # 딱 한 번만 열렸다


async def test_default_tools_aclose_closes_the_shared_store(monkeypatch):
    from knowledge_forge.search.retrieve import RetrievalResult

    exited = []

    class _FakeConnectedStoreWithExitTracking:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            exited.append(True)
            return False

    async def _fake_retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, **kwargs):
        return RetrievalResult(query, {}, [], [], [], [], [], None, [], 0.0)

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr(
        "knowledge_forge.storage.store.PipelineStore", lambda *a, **k: _FakeConnectedStoreWithExitTracking()
    )
    monkeypatch.setattr("knowledge_forge.agent.tools.retrieve", _fake_retrieve)

    tools = default_tools()
    search_tool = next(t for t in tools if t.name == "search")
    await search_tool.handler({"query": "q"})

    await tools.aclose()

    assert exited == [True]


async def test_default_tools_aclose_is_a_no_op_without_a_database_url():
    """DB가 없어 도구 세트가 애초에 store를 연 적이 없으면 `aclose()`는 그냥 아무것도
    안 한다(닫을 게 없으므로 예외도 안 난다)."""
    tools = default_tools()
    await tools.aclose()


async def test_delegate_task_closes_its_own_child_tool_set_store(monkeypatch):
    """`delegate_task`의 자식 도구 세트는 그 호출 하나에서만 쓰이고 버려진다 - 자기 몫의
    `_LazyPipelineStore`를 직접 닫아야 한다(부모/세션의 것은 안 건드리고)."""
    from knowledge_forge.search.retrieve import RetrievalResult

    exited = []

    class _FakeConnectedStoreWithExitTracking:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            exited.append(True)
            return False

    async def _fake_retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, **kwargs):
        return RetrievalResult(query, {}, [], [], [], [], [], None, [], 0.0)

    class _RecordingProvider(ChatProvider):
        async def send(self, messages, tools, system) -> AssistantTurn:
            # 자식 대화 안에서 search 도구를 한 번 부르게 해 store가 실제로 열리게 한다.
            if not any(m.role == "tool" for m in messages):
                from knowledge_forge.agent.providers.base import ToolCall

                return AssistantTurn(
                    text=None, tool_calls=[ToolCall(id="c1", name="search", arguments={"query": "q"})], stop_reason="tool_use"
                )
            return AssistantTurn(text="done", tool_calls=[], stop_reason="end_turn")

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr(
        "knowledge_forge.storage.store.PipelineStore", lambda *a, **k: _FakeConnectedStoreWithExitTracking()
    )
    monkeypatch.setattr("knowledge_forge.agent.tools.retrieve", _fake_retrieve)

    provider = _RecordingProvider()
    delegate = next(t for t in default_tools(provider=provider, system="system prompt") if t.name == "delegate_task")

    await delegate.handler({"task": "investigate project X"})

    assert exited == [True]


async def test_default_tools_search_ignores_a_model_supplied_project_id(monkeypatch):
    """결함8(`wiki/search-code-review-2026-09-14.md`): `args.get("project_id") or project_id`가
    모델이 지어낸 값(관측: "NH_MVP", "project_123")에 세션 값을 내줘 0건 응답이 났다 -
    스키마에서 `project_id`를 빼고, 핸들러는 세션 값만 쓴다."""
    captured = {}

    async def _fake_retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, **kwargs):
        captured["project_id"] = project_id
        from knowledge_forge.search.retrieve import RetrievalResult

        return RetrievalResult(query, {}, [], [], [], [], [], None, [], 0.0)

    monkeypatch.setattr(settings, "database_url", "postgresql://kf:kf@localhost/kf")
    monkeypatch.setattr("knowledge_forge.storage.store.PipelineStore", lambda *a, **k: _FakeConnectedStore(object()))
    monkeypatch.setattr("knowledge_forge.agent.tools.retrieve", _fake_retrieve)

    tool = next(t for t in default_tools(project_id="session-proj") if t.name == "search")
    assert "project_id" not in tool.input_schema["properties"]
    # 스키마에 없는 필드라도 모델이 그래도 실어 보낼 수 있다 - 핸들러가 args를 아예 안 봐야 한다.
    await tool.handler({"query": "q", "project_id": "invented-by-model"})

    assert captured["project_id"] == "session-proj"


_TEST_DSN = os.environ.get("KF_TEST_DATABASE_URL")


@pytest.mark.skipif(not _TEST_DSN, reason="KF_TEST_DATABASE_URL not set - no Postgres to test against")
async def test_read_document_tool_reads_a_real_document_in_order_and_pages_with_next_offset(monkeypatch, tmp_path):
    """End-to-end through the real tool handler and a real PipelineStore -
    seeds under a test-only prefix and cleans up, since the dev DB holds
    real data."""
    from datetime import datetime, timezone

    from knowledge_forge.models.chunk import Chunk, HierarchyLevel
    from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
    from knowledge_forge.models.source import Source, SourceRef, SourceType
    from knowledge_forge.storage.postgres_store import PostgresStore

    src = Source(
        source_id="test-rdt-s1",
        source_type=SourceType.LOCAL_FILE,
        uri="file:///meeting.md",
        content_hash="h-rdt",
        collected_at=datetime.now(timezone.utc),
        project_id="test-read-document-tool",
    )
    doc = Document(
        document_id="test-rdt-d1",
        source=SourceRef.from_source(src),
        elements=[TextBlock(element_id="e1", text="x")],
        metadata=DocumentMetadata(title="회의록"),
    )
    chunks = [
        Chunk(
            chunk_id=f"test-rdt-c{i}",
            document_id="test-rdt-d1",
            source_id="test-rdt-s1",
            hierarchy_level=HierarchyLevel.PARAGRAPH,
            sequence_index=i,
            text=f"{i}번째 안건 " * 10,
            token_count=10,
            element_ids=[f"e{i}"],
        )
        for i in (2, 0, 1)
    ]
    async with PostgresStore(_TEST_DSN) as db:
        await db.init_schema()
        await db.save_source(src, None)
        await db.save_document(doc)
        await db.save_chunks(chunks)
    try:
        monkeypatch.setattr(settings, "database_url", _TEST_DSN)
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        tool = next(t for t in default_tools() if t.name == "read_document")

        first = await tool.handler({"document_id": "test-rdt-d1", "max_chars": len("0번째 안건 " * 10) + 1})
        assert first["title"] == "회의록"
        assert [c["sequence_index"] for c in first["chunks"]] == [0]
        assert first["truncated"] is True and first["next_offset"] == 1

        rest = await tool.handler({"document_id": "test-rdt-d1", "offset": first["next_offset"]})
        assert [c["sequence_index"] for c in rest["chunks"]] == [1, 2]
        assert rest["truncated"] is False

        with pytest.raises(LookupError):
            await tool.handler({"document_id": "test-rdt-missing"})
    finally:
        async with PostgresStore(_TEST_DSN) as db:
            await db.conn.execute("DELETE FROM chunks WHERE chunk_id LIKE 'test-rdt-%'")
            await db.conn.execute("DELETE FROM documents WHERE document_id LIKE 'test-rdt-%'")
            await db.conn.execute("DELETE FROM sources WHERE source_id LIKE 'test-rdt-%'")
