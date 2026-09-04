"""get_embedding_provider() picks the class named by KF_EMBEDDING_PROVIDER -
same shape as tests/test_provider_factory.py does for get_extractor(), and
the same monkeypatching of the module-level `settings` singleton the factory
reads from.
"""

import pytest

from knowledge_forge.config import Settings
from knowledge_forge.embedding.factory import get_embedding_provider
from knowledge_forge.embedding.openai_provider import OpenAIEmbeddingProvider
from knowledge_forge.embedding.openrouter_provider import OpenRouterEmbeddingProvider


def test_defaults_to_openai(monkeypatch):
    monkeypatch.setattr("knowledge_forge.embedding.factory.settings", Settings())
    assert isinstance(get_embedding_provider(), OpenAIEmbeddingProvider)


def test_returns_openrouter_when_configured(monkeypatch):
    monkeypatch.setattr(
        "knowledge_forge.embedding.factory.settings",
        Settings(embedding_provider="openrouter", embedding_model="openai/text-embedding-3-small"),
    )
    provider = get_embedding_provider()
    assert isinstance(provider, OpenRouterEmbeddingProvider)
    assert provider._model == "openai/text-embedding-3-small"


def test_explicit_model_argument_wins_over_the_setting(monkeypatch):
    monkeypatch.setattr(
        "knowledge_forge.embedding.factory.settings",
        Settings(embedding_provider="openrouter", embedding_model="openai/text-embedding-3-small"),
    )
    assert get_embedding_provider(model="qwen/qwen3-embedding-8b")._model == "qwen/qwen3-embedding-8b"


def test_unknown_provider_raises(monkeypatch):
    settings = Settings()
    # Bypasses the Literal on purpose - the factory's final `raise` is the
    # guard for a provider added to config.py but not wired up here.
    object.__setattr__(settings, "embedding_provider", "voyage")
    monkeypatch.setattr("knowledge_forge.embedding.factory.settings", settings)

    with pytest.raises(ValueError, match="voyage"):
        get_embedding_provider()
