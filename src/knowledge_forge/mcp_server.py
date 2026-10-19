"""Read-only MCP server over the knowledge graph (wiki/2026-10-19-mcp-server.md,
improvement-plan O6) - the interface a second agent consumes, alongside the
built-in agent (agent/) and the HTTP API (api/).

Four tools - `lookup_entity`, `get_object`, `neighbors`, `search` - and
every one is pinned to the single `project_id` fixed when the server starts
(`knowledge-forge mcp --project-id`). No tool takes a project argument, so
a client can't widen its own scope by asking; storage enforces the same
scope underneath (an object of another project reads as not found).

The tool logic is agent/tools.py's ToolSpec handlers (`read_only_tools`),
which already clamp limits and shape results for an LLM consumer - this
module only gives them typed signatures for the MCP SDK's schema
generation and maps "not found"/bad input to a tool error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from knowledge_forge.agent.providers.base import ToolSpec
from knowledge_forge.agent.tools import read_only_tools

_INSTRUCTIONS = (
    "knowledge-forge 사내 지식 그래프(읽기 전용, 프로젝트 {project_id}). 특정 인물·조직 기준이면 "
    "lookup_entity, 결정·이슈의 원인·결과는 neighbors, 그 밖의 질문은 search부터. "
    "결과의 document_id/chunk_ids가 근거다."
)


def build_server(database_url: str, project_id: str) -> MCPServer:
    """The server for one project. Its DB connection opens on the first tool
    call and closes when the server's lifespan ends (`run()` returning)."""
    tools = read_only_tools(database_url, project_id)
    specs: dict[str, ToolSpec] = {t.name: t for t in tools}

    @asynccontextmanager
    async def _lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await tools.aclose()

    server = MCPServer(
        name="knowledge-forge",
        instructions=_INSTRUCTIONS.format(project_id=project_id),
        lifespan=_lifespan,
    )

    async def _call(name: str, args: dict[str, Any]) -> Any:
        try:
            return await specs[name].handler({k: v for k, v in args.items() if v is not None})
        except (LookupError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    async def lookup_entity(name: str, entity_type: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        return await _call("lookup_entity", {"name": name, "entity_type": entity_type, "limit": limit})

    async def get_object(object_id: str) -> dict[str, Any]:
        return await _call("get_object", {"object_id": object_id})

    async def neighbors(
        object_id: str, depth: int = 1, relation_types: list[str] | None = None, limit: int = 20
    ) -> dict[str, Any]:
        return await _call(
            "neighbors", {"object_id": object_id, "depth": depth, "relation_types": relation_types, "limit": limit}
        )

    async def search(query: str, limit: int = 10) -> dict[str, Any]:
        return await _call("search", {"query": query, "limit": limit})

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    for fn in (lookup_entity, get_object, neighbors, search):
        server.add_tool(fn, description=specs[fn.__name__].description, annotations=read_only)
    server.aclose = tools.aclose  # for callers that drive tools without running the server (tests)
    return server
