"""Verifies get_extractor()/get_vision_extractor() pick the right provider
class based on settings.llm_provider - each test overrides the shared
module-level `settings` singleton for its duration via monkeypatch, since
that's what both factories read from.
"""

from knowledge_forge.config import Settings
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.factory import get_extractor
from knowledge_forge.extraction.openrouter_extractor import OpenRouterExtractor
from knowledge_forge.vision.claude_agent_sdk_extractor import ClaudeAgentSDKVisionExtractor
from knowledge_forge.vision.extractor import VisionExtractor
from knowledge_forge.vision.factory import get_vision_extractor
from knowledge_forge.vision.openrouter_extractor import OpenRouterVisionExtractor


def test_get_extractor_defaults_to_anthropic(monkeypatch):
    monkeypatch.setattr("knowledge_forge.extraction.factory.settings", Settings(llm_provider="anthropic"))
    assert isinstance(get_extractor(), ClaudeExtractor)


def test_get_extractor_returns_openrouter_when_configured(monkeypatch):
    monkeypatch.setattr("knowledge_forge.extraction.factory.settings", Settings(llm_provider="openrouter"))
    extractor = get_extractor()
    assert isinstance(extractor, OpenRouterExtractor)


def test_get_vision_extractor_defaults_to_anthropic(monkeypatch):
    monkeypatch.setattr("knowledge_forge.vision.factory.settings", Settings(llm_provider="anthropic"))
    assert isinstance(get_vision_extractor(), VisionExtractor)


def test_get_vision_extractor_returns_openrouter_when_configured(monkeypatch):
    monkeypatch.setattr("knowledge_forge.vision.factory.settings", Settings(llm_provider="openrouter"))
    extractor = get_vision_extractor()
    assert isinstance(extractor, OpenRouterVisionExtractor)


def test_get_vision_extractor_returns_claude_agent_sdk_when_configured(monkeypatch):
    """Before this fix, "claude_agent_sdk" fell through to plain
    VisionExtractor (the direct Anthropic SDK path, needing
    ANTHROPIC_API_KEY) - the exact bug wiki/vision-extraction.md documents
    (869 silently-failed VLM calls on a machine with no such key)."""
    monkeypatch.setattr("knowledge_forge.vision.factory.settings", Settings(llm_provider="claude_agent_sdk"))
    extractor = get_vision_extractor()
    assert isinstance(extractor, ClaudeAgentSDKVisionExtractor)


def test_get_extractor_openrouter_uses_configured_model(monkeypatch):
    monkeypatch.setattr(
        "knowledge_forge.extraction.factory.settings",
        Settings(llm_provider="openrouter", openrouter_extraction_model="openai/gpt-4o-mini"),
    )
    extractor = get_extractor()
    assert extractor._model == "openai/gpt-4o-mini"
