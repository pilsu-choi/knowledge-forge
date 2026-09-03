from knowledge_forge.agent.plan import PlanState
from knowledge_forge.agent.tools import default_tools


async def test_default_tools_wraps_file_search_and_forwards_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20):
        captured.update(query=query, project_id=project_id, type=type, limit=limit)
        return [{"path": "p"}]

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tools = default_tools()
    assert [t.name for t in tools] == ["file_search", "write_plan"]

    spec = tools[0]
    result = await spec.handler({"query": "auth", "project_id": "proj", "type": "decision", "limit": 5})

    assert result == [{"path": "p"}]
    assert captured == {"query": "auth", "project_id": "proj", "type": "decision", "limit": 5}


async def test_default_tools_handler_uses_defaults_for_optional_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20):
        captured.update(query=query, project_id=project_id, type=type, limit=limit)
        return []

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    await default_tools()[0].handler({"query": "auth"})

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
