from knowledge_forge.agent.tools import default_tools


async def test_default_tools_wraps_file_search_and_forwards_arguments(monkeypatch):
    captured = {}

    async def _fake_file_search(query, project_id=None, type=None, limit=20):
        captured.update(query=query, project_id=project_id, type=type, limit=limit)
        return [{"path": "p"}]

    monkeypatch.setattr("knowledge_forge.agent.tools.file_search", _fake_file_search)

    tools = default_tools()
    assert [t.name for t in tools] == ["file_search"]

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
