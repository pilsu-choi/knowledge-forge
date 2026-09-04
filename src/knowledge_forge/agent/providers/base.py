"""Re-exports `knowledge_forge.chat_provider` unchanged - kept as the import
path every `agent/*` module already uses (`agent/loop.py`, `agent/tools.py`,
`agent/providers/*`, ...). The actual definitions moved to
`knowledge_forge.chat_provider` so `search/planner.py` can use `ChatProvider`/
`Message` without importing the `agent` package - see that module's
docstring for why (circular import + layering violation)."""

from knowledge_forge.chat_provider import (
    AssistantTurn,
    ChatProvider,
    Message,
    ToolCall,
    ToolSpec,
)

__all__ = ["AssistantTurn", "ChatProvider", "Message", "ToolCall", "ToolSpec"]
