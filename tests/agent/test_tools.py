from knowledge_forge.agent.plan import PlanState
from knowledge_forge.agent.providers.base import AssistantTurn, ChatProvider, Message
from knowledge_forge.agent.tools import default_tools

_EXPECTED_BASE_TOOL_NAMES = [
    "file_search",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "write_memory",
    "write_plan",
]


async def test_default_tools_wraps_file_search_and_forwards_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20):
        captured.update(query=query, project_id=project_id, type=type, limit=limit)
        return [{"path": "p"}]

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tools = default_tools()
    assert [t.name for t in tools] == _EXPECTED_BASE_TOOL_NAMES

    spec = next(t for t in tools if t.name == "file_search")
    result = await spec.handler({"query": "auth", "project_id": "proj", "type": "decision", "limit": 5})

    assert result == [{"path": "p"}]
    assert captured == {"query": "auth", "project_id": "proj", "type": "decision", "limit": 5}


async def test_default_tools_handler_uses_defaults_for_optional_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20):
        captured.update(query=query, project_id=project_id, type=type, limit=limit)
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    file_search_tool = next(t for t in default_tools() if t.name == "file_search")
    await file_search_tool.handler({"query": "auth"})

    assert captured == {"query": "auth", "project_id": None, "type": None, "limit": 20}


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
