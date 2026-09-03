from types import SimpleNamespace

import httpx2
import openai
import pytest

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
    data = [SimpleNamespace(embedding=vector) for vector in vectors]
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
    provider = OpenAIEmbeddingProvider(client=client, model="text-embedding-3-small")

    result = await provider.embed(["first text", "second text"])

    assert result == vectors
    assert client.embeddings.calls == 1
    assert client.embeddings.last_kwargs["model"] == "text-embedding-3-small"
    assert client.embeddings.last_kwargs["input"] == ["first text", "second text"]


async def test_retries_once_on_rate_limit_then_succeeds():
    vectors = [[0.1, 0.2]]
    client = _FakeClient([_rate_limit_error(), _embeddings_response(vectors)])
    provider = OpenAIEmbeddingProvider(client=client)

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
