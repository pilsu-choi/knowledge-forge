"""Picks the ChatProvider implementation based on KF_AGENT_PROVIDER - the one
switch point agent/runtime.py should use instead of hardcoding
AnthropicChatProvider, mirroring extraction/factory.py::get_extractor().

"claude_agent_sdk" is selectable *only* by passing `provider=` explicitly
(the offline eval commands' `--provider`), never through KF_AGENT_PROVIDER:
`settings.agent_provider`'s Literal deliberately excludes it, because that
SDK owns its own tool-call loop and cannot back the harness
(wiki/agent-runtime.md). The class it returns refuses any call carrying
tools, so the exclusion holds even if something else reaches for it.
"""

from knowledge_forge.agent.providers.anthropic import AnthropicChatProvider
from knowledge_forge.agent.providers.base import ChatProvider
from knowledge_forge.agent.providers.claude_agent_sdk import ClaudeAgentSDKChatProvider
from knowledge_forge.agent.providers.openai_compatible import OpenAICompatibleChatProvider
from knowledge_forge.config import settings


def get_chat_provider(provider: str | None = None, model: str | None = None) -> ChatProvider:
    active = provider or settings.agent_provider
    if active == "claude_agent_sdk":
        return ClaudeAgentSDKChatProvider(model=model or settings.claude_agent_sdk_extraction_model)
    if active == "openai_compatible":
        return OpenAICompatibleChatProvider(
            model=model or settings.agent_openai_compatible_model,
            base_url=settings.agent_openai_compatible_base_url,
        )
    return AnthropicChatProvider(model=model or settings.agent_model)
