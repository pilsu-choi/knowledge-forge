"""Fakes shared by the eval tests - a scripted ChatProvider and a SQL-shape-dispatching store."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider, ToolCall


class ScriptedProvider(ChatProvider):
    """Returns queued turns in order: a str is a plain text turn, a list of (name, args) is a tool-call turn."""

    def __init__(self, *turns: Any) -> None:
        self.turns = list(turns)
        self.calls: list[tuple[str, str]] = []

    async def send(self, messages, tools, system) -> AssistantTurn:
        self.calls.append((system, messages[-1].content or ""))
        turn = self.turns.pop(0)
        if isinstance(turn, (dict, list)) and not (isinstance(turn, list) and turn and isinstance(turn[0], tuple)):
            return AssistantTurn(text=json.dumps(turn, ensure_ascii=False), tool_calls=[], stop_reason="end_turn")
        if isinstance(turn, list):
            calls = [ToolCall(id=f"t{i}", name=name, arguments=args) for i, (name, args) in enumerate(turn)]
            return AssistantTurn(text=None, tool_calls=calls, stop_reason="tool_use")
        return AssistantTurn(text=turn, tool_calls=[], stop_reason="end_turn")


class FakeStore:
    """`sql` maps a substring of the SQL to the rows it returns (first match wins);
    `chunks` maps document_id to (chunk_id, text) pairs for `list_chunks_by_document`."""

    def __init__(self, sql: dict[str, list[tuple]] | None = None, chunks: dict[str, list[tuple[str, str]]] | None = None) -> None:
        self.sql = sql or {}
        self.chunks = chunks or {}
        self.queries: list[str] = []

    async def execute_readonly_query(self, sql: str, limit: int):
        self.queries.append(sql)
        for needle, rows in self.sql.items():
            if needle in sql:
                return list(rows), []
        return [], []

    async def list_chunks_by_document(self, document_id: str, *, hierarchy_level=None):
        return [SimpleNamespace(chunk_id=cid, text=text) for cid, text in self.chunks.get(document_id, [])]

    async def search_documents_by_title(self, query, **kwargs):
        return []

    async def search_chunks_lexical(self, query, **kwargs):
        return []


FINGERPRINT = {
    "embeddings": {"chunk": 3, "knowledge_object": 0},
    "documents": {"count": 2, "hash": "d"},
    "chunks": {"count": 3, "hash": "c"},
    "knowledge_objects": {"count": 0, "hash": "k"},
}


@pytest.fixture
def fingerprint() -> dict[str, Any]:
    return json.loads(json.dumps(FINGERPRINT))


@pytest.fixture
def fakes() -> SimpleNamespace:
    """tests/ is not a package, so test modules get the fakes through this fixture instead of importing conftest."""
    return SimpleNamespace(FakeStore=FakeStore, ScriptedProvider=ScriptedProvider, FINGERPRINT=FINGERPRINT)
