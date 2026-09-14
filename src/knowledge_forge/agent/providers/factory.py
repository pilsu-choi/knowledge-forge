"""Picks the ChatProvider implementation based on KF_AGENT_PROVIDER - the one
switch point agent/runtime.py should use instead of hardcoding
AnthropicChatProvider, mirroring extraction/factory.py::get_extractor().
"""

from knowledge_forge.agent.providers.anthropic import AnthropicChatProvider
from knowledge_forge.agent.providers.base import ChatProvider
from knowledge_forge.agent.providers.openai_compatible import OpenAICompatibleChatProvider
from knowledge_forge.config import settings


def get_chat_provider(provider: str | None = None, model: str | None = None) -> ChatProvider:
    active = provider or settings.agent_provider
    if active == "openai_compatible":
        return OpenAICompatibleChatProvider(
            model=model or settings.agent_openai_compatible_model,
            base_url=settings.agent_openai_compatible_base_url,
        )
    return AnthropicChatProvider(model=model or settings.agent_model)
