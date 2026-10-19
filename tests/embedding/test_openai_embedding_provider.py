from types import SimpleNamespace

import httpx2
import openai
import pytest

from knowledge_forge.config import Settings
from knowledge_forge.embedding.openai_provider import (
    OPENAI_EMBEDDING_CREDENTIAL_HINT_KO,
    OpenAIEmbeddingProvider,
)
from knowledge_forge.exceptions import EmbeddingError

_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")


def _rate_limit_error() -> openai.RateLimitError:
    response = httpx2.Response(429, request=_REQUEST, json={"error": {"message": "slow down"}})
    return openai.RateLimitError("slow down", response=response, body=None)


def _bad_request_error() -> openai.BadRequestError:
    response = httpx2.Response(400, request=_REQUEST, json={"error": {"message": "bad input"}})
    return openai.BadRequestError("bad input", response=response, body=None)


def _authentication_error() -> openai.AuthenticationError:
    response = httpx2.Response(401, request=_REQUEST, json={"error": {"message": "invalid api key"}})
    return openai.AuthenticationError("invalid api key", response=response, body=None)


def _embeddings_response(vectors: list[list[float]]):
    data = [SimpleNamespace(embedding=vector, index=i) for i, vector in enumerate(vectors)]
    return SimpleNamespace(data=data)


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


async def test_embed_returns_vectors_in_the_same_order_as_input():
    vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    client = _FakeClient([_embeddings_response(vectors)])
    # expected_dimensions has to match the fake vectors here: embed() now
    # guards the model's output width against KF_EMBEDDING_DIMENSIONS (1536
    # by default), so 3-float test vectors would otherwise be rejected.
    provider = OpenAIEmbeddingProvider(client=client, model="text-embedding-3-small", expected_dimensions=3)

    result = await provider.embed(["first text", "second text"])

    assert result == vectors
    assert client.embeddings.calls == 1
    assert client.embeddings.last_kwargs["model"] == "text-embedding-3-small"
    assert client.embeddings.last_kwargs["input"] == ["first text", "second text"]


async def test_retries_once_on_rate_limit_then_succeeds():
    vectors = [[0.1, 0.2]]
    client = _FakeClient([_rate_limit_error(), _embeddings_response(vectors)])
    provider = OpenAIEmbeddingProvider(client=client, expected_dimensions=2)

    result = await provider.embed(["some text"])

    assert result == vectors
    assert client.embeddings.calls == 2


async def test_bad_request_is_not_retried_and_raises_embedding_error():
    client = _FakeClient([_bad_request_error()])
    provider = OpenAIEmbeddingProvider(client=client)

    with pytest.raises(EmbeddingError):
        await provider.embed(["some text"])

    assert client.embeddings.calls == 1


async def test_authentication_error_is_wrapped_with_korean_hint():
    client = _FakeClient([_authentication_error()])
    provider = OpenAIEmbeddingProvider(client=client)

    with pytest.raises(EmbeddingError) as exc_info:
        await provider.embed(["some text"])

    assert str(exc_info.value) == OPENAI_EMBEDDING_CREDENTIAL_HINT_KO
    assert client.embeddings.calls == 1


async def test_dimension_mismatch_raises_with_the_model_and_both_dimensions():
    """The guard that keeps a wrongly-sized model from reaching pgvector -
    see embedding/validation.py for why this is worth failing early on."""
    client = _FakeClient([_embeddings_response([[0.1, 0.2, 0.3]])])
    provider = OpenAIEmbeddingProvider(client=client, model="text-embedding-3-large", expected_dimensions=1536)

    with pytest.raises(EmbeddingError) as exc_info:
        await provider.embed(["some text"])

    message = str(exc_info.value)
    assert "text-embedding-3-large" in message
    assert "1536" in message
    assert "3차원" in message
    assert "schema.sql" in message


async def test_response_shorter_than_input_raises_instead_of_misaligning():
    client = _FakeClient([_embeddings_response([[0.1, 0.2]])])
    provider = OpenAIEmbeddingProvider(client=client, expected_dimensions=2)

    with pytest.raises(EmbeddingError) as exc_info:
        await provider.embed(["first text", "second text"])

    assert "2개" in str(exc_info.value)


def test_api_key_precedence_prefers_the_constructor_argument(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-environ")
    monkeypatch.setattr(
        "knowledge_forge.embedding.openai_provider.settings",
        Settings(embedding_api_key="from-dotenv"),
    )

    provider = OpenAIEmbeddingProvider(api_key="from-constructor")

    assert provider._client.api_key == "from-constructor"


def test_api_key_falls_back_to_kf_embedding_api_key_before_the_environment(monkeypatch):
    """KF_EMBEDDING_API_KEY is the only one of the three a `.env` file can
    actually supply (config.py's Settings docstring) - it has to win over the
    plain env var, otherwise an exported stale key would shadow it."""
    monkeypatch.setenv("OPENAI_API_KEY", "from-environ")
    monkeypatch.setattr(
        "knowledge_forge.embedding.openai_provider.settings",
        Settings(embedding_api_key="from-dotenv"),
    )

    provider = OpenAIEmbeddingProvider()

    assert provider._client.api_key == "from-dotenv"


def test_api_key_falls_back_to_openai_api_key_environment_variable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-environ")
    monkeypatch.setattr("knowledge_forge.embedding.openai_provider.settings", Settings(embedding_api_key=None))

    provider = OpenAIEmbeddingProvider()

    assert provider._client.api_key == "from-environ"


def test_base_url_defaults_to_the_openai_sdk_default_and_is_overridable(monkeypatch):
    monkeypatch.setattr("knowledge_forge.embedding.openai_provider.settings", Settings())
    assert "api.openai.com" in str(OpenAIEmbeddingProvider(api_key="k")._client.base_url)

    monkeypatch.setattr(
        "knowledge_forge.embedding.openai_provider.settings",
        Settings(embedding_base_url="http://localhost:8000/v1"),
    )
    # KF_EMBEDDING_BASE_URL is what makes a self-hosted OpenAI-compatible
    # server (vLLM/TEI) usable without a third provider class.
    assert str(OpenAIEmbeddingProvider(api_key="k")._client.base_url).startswith("http://localhost:8000/v1")


def test_schema_sql_fills_in_the_configured_embedding_dimensions(monkeypatch):
    from knowledge_forge.config import settings
    from knowledge_forge.storage.postgres_store import schema_sql

    monkeypatch.setattr(settings, "embedding_dimensions", 1024)
    sql = schema_sql()
    assert "vector(1024)" in sql
    assert "__EMBEDDING_DIMENSIONS__" not in sql.replace("-- ", "").split("CREATE TABLE IF NOT EXISTS knowledge_object_embeddings")[1]


def test_embedding_source_defaults_to_chunk():
    from knowledge_forge.config import Settings

    assert Settings(_env_file=None).embedding_source == "chunk"
