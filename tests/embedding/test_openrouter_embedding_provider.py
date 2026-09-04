"""OpenRouterEmbeddingProvider unit tests - the `openai` SDK client is mocked
throughout, same as tests/extraction/test_openrouter_extractor.py and for the
same reason (no OPENROUTER_API_KEY in this environment, see
wiki/llm-providers.md's "검증 상태" section).
"""

from types import SimpleNamespace

import httpx2
import openai
import pytest

from knowledge_forge.config import Settings
from knowledge_forge.embedding.openrouter_provider import (
    OPENROUTER_EMBEDDING_CREDENTIAL_HINT_KO,
    OpenRouterEmbeddingProvider,
)
from knowledge_forge.exceptions import EmbeddingError

_REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/embeddings")


def _rate_limit_error() -> openai.RateLimitError:
    response = httpx2.Response(429, request=_REQUEST, json={"error": {"message": "slow down"}})
    return openai.RateLimitError("slow down", response=response, body=None)


def _authentication_error() -> openai.AuthenticationError:
    response = httpx2.Response(401, request=_REQUEST, json={"error": {"message": "invalid api key"}})
    return openai.AuthenticationError("invalid api key", response=response, body=None)


def _response(items):
    return SimpleNamespace(data=list(items))


def _item(vector: list[float], index: int | None = None):
    return SimpleNamespace(embedding=vector, index=index) if index is not None else SimpleNamespace(embedding=vector)


class _FakeEmbeddings:
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class _FakeClient:
    def __init__(self, side_effects):
        self.embeddings = _FakeEmbeddings(side_effects)


async def test_embed_sends_one_batched_request_without_a_dimensions_parameter():
    client = _FakeClient([_response([_item([0.1, 0.2], 0), _item([0.3, 0.4], 1)])])
    provider = OpenRouterEmbeddingProvider(
        client=client, model="openai/text-embedding-3-small", expected_dimensions=2
    )

    result = await provider.embed(["first", "second"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert client.embeddings.calls == 1
    assert client.embeddings.last_kwargs["model"] == "openai/text-embedding-3-small"
    assert client.embeddings.last_kwargs["input"] == ["first", "second"]
    # OpenRouter's /embeddings docs don't list `dimensions`; sending an
    # undocumented parameter through a gateway has unknown per-model
    # behaviour, so the width is only verified, never requested.
    assert "dimensions" not in client.embeddings.last_kwargs


async def test_out_of_order_response_is_realigned_by_index():
    """The gateway relays another vendor's response, so input order is a
    second-hand guarantee - `index` is authoritative when present."""
    client = _FakeClient([_response([_item([0.3, 0.4], 1), _item([0.1, 0.2], 0)])])
    provider = OpenRouterEmbeddingProvider(client=client, expected_dimensions=2)

    assert await provider.embed(["first", "second"]) == [[0.1, 0.2], [0.3, 0.4]]


async def test_response_without_index_falls_back_to_positional_order():
    client = _FakeClient([_response([_item([0.1, 0.2]), _item([0.3, 0.4])])])
    provider = OpenRouterEmbeddingProvider(client=client, expected_dimensions=2)

    assert await provider.embed(["first", "second"]) == [[0.1, 0.2], [0.3, 0.4]]


async def test_response_shorter_than_input_raises_instead_of_misaligning():
    client = _FakeClient([_response([_item([0.1, 0.2], 0)])])
    provider = OpenRouterEmbeddingProvider(client=client, expected_dimensions=2)

    with pytest.raises(EmbeddingError) as exc_info:
        await provider.embed(["first", "second"])

    assert "2개" in str(exc_info.value)


async def test_dimension_mismatch_raises_with_the_model_and_both_dimensions():
    client = _FakeClient([_response([_item([0.1] * 3072, 0)])])
    provider = OpenRouterEmbeddingProvider(
        client=client, model="openai/text-embedding-3-large", expected_dimensions=1536
    )

    with pytest.raises(EmbeddingError) as exc_info:
        await provider.embed(["some text"])

    message = str(exc_info.value)
    assert "openai/text-embedding-3-large" in message
    assert "3072차원" in message
    assert "1536" in message
    assert "schema.sql" in message


async def test_retries_once_on_rate_limit_then_succeeds():
    client = _FakeClient([_rate_limit_error(), _response([_item([0.1, 0.2], 0)])])
    provider = OpenRouterEmbeddingProvider(client=client, expected_dimensions=2)

    assert await provider.embed(["some text"]) == [[0.1, 0.2]]
    assert client.embeddings.calls == 2


async def test_authentication_error_is_wrapped_with_korean_hint():
    client = _FakeClient([_authentication_error()])
    provider = OpenRouterEmbeddingProvider(client=client)

    with pytest.raises(EmbeddingError) as exc_info:
        await provider.embed(["some text"])

    assert str(exc_info.value) == OPENROUTER_EMBEDDING_CREDENTIAL_HINT_KO
    assert "KF_EMBEDDING_API_KEY" in str(exc_info.value)
    assert client.embeddings.calls == 1


def test_api_key_precedence_prefers_the_constructor_argument(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-environ")
    monkeypatch.setattr(
        "knowledge_forge.embedding.openrouter_provider.settings",
        Settings(embedding_api_key="from-dotenv"),
    )

    assert OpenRouterEmbeddingProvider(api_key="from-constructor")._client.api_key == "from-constructor"


def test_api_key_falls_back_to_kf_embedding_api_key_before_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-environ")
    monkeypatch.setattr(
        "knowledge_forge.embedding.openrouter_provider.settings",
        Settings(embedding_api_key="from-dotenv"),
    )

    assert OpenRouterEmbeddingProvider()._client.api_key == "from-dotenv"


def test_api_key_falls_back_to_openrouter_api_key_environment_variable(monkeypatch):
    """Backwards compatibility: an exported OPENROUTER_API_KEY (the extraction
    path's existing convention) keeps working with no .env involved."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-environ")
    monkeypatch.setattr("knowledge_forge.embedding.openrouter_provider.settings", Settings(embedding_api_key=None))

    assert OpenRouterEmbeddingProvider()._client.api_key == "from-environ"


def test_base_url_defaults_to_openrouter_and_is_overridable(monkeypatch):
    monkeypatch.setattr("knowledge_forge.embedding.openrouter_provider.settings", Settings())
    assert str(OpenRouterEmbeddingProvider(api_key="k")._client.base_url).startswith("https://openrouter.ai/api/v1")

    monkeypatch.setattr(
        "knowledge_forge.embedding.openrouter_provider.settings",
        Settings(embedding_base_url="https://gateway.internal/v1"),
    )
    assert str(OpenRouterEmbeddingProvider(api_key="k")._client.base_url).startswith("https://gateway.internal/v1")
