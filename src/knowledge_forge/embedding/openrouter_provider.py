"""Embedding generation via OpenRouter's OpenAI-compatible `/embeddings`
endpoint (`POST https://openrouter.ai/api/v1/embeddings`, Bearer auth).

Same relationship to OpenAIEmbeddingProvider that
extraction/openrouter_extractor.py has to extraction/claude_extractor.py: an
alternative backend selected by config (`KF_EMBEDDING_PROVIDER=openrouter`),
not a replacement. It exists because OpenRouter fronts embedding models from
several vendors behind one key - the same "model choice flexibility, not cost"
motivation wiki/llm-providers.md gives for the extraction path.

The wire protocol is OpenAI's, so this reuses the `openai` SDK with nothing
but `base_url` swapped, exactly like OpenRouterExtractor does for chat
completions. Two deliberate differences from OpenAIEmbeddingProvider:

- `dimensions` is never sent. OpenAI's API accepts it for the
  text-embedding-3-* family, but OpenRouter's `/embeddings` documentation
  does not list it, and an unknown parameter's behaviour across the models
  it routes to is unknown (silently ignored vs 400). The dimension is
  therefore only *verified* (validation.py), never *requested*.
- The response is re-ordered by `index` when the field is present - see
  `_call_with_retry`.
"""

import asyncio
import os

import openai

from knowledge_forge.config import settings
from knowledge_forge.embedding.base import EmbeddingProvider
from knowledge_forge.embedding.validation import ensure_expected_dimensions, ensure_one_vector_per_text
from knowledge_forge.exceptions import EmbeddingError
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry

# OpenRouter slugs are `vendor/model` (wiki/llm-providers.md's "모델 슬러그
# 형식") - a bare "text-embedding-3-small" is NOT a valid slug here, which is
# why this default differs from OpenAIEmbeddingProvider's by the prefix alone.
DEFAULT_MODEL = "openai/text-embedding-3-small"

OPENROUTER_EMBEDDING_CREDENTIAL_HINT_KO = (
    "OpenRouter API 자격 증명을 찾을 수 없습니다.\n"
    "  .env에 KF_EMBEDDING_API_KEY=sk-or-... 를 적거나 OPENROUTER_API_KEY 환경변수를 설정하세요"
    " (https://openrouter.ai/keys 에서 발급).\n"
    "  주의: .env의 OPENROUTER_API_KEY는 KF_ 접두가 아니라서 읽히지 않습니다 - 자세한 이유는"
    " config.py의 Settings docstring 참고."
)


class OpenRouterEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        client: openai.AsyncOpenAI | None = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_concurrency: int | None = None,
        base_url: str | None = None,
        expected_dimensions: int | None = None,
    ):
        # Credential precedence (identical to OpenAIEmbeddingProvider's, only
        # the env var name differs): constructor arg > KF_EMBEDDING_API_KEY
        # (the only one of the three that a `.env` file can actually supply -
        # see config.py's Settings docstring) > an exported OPENROUTER_API_KEY,
        # kept last for backwards compatibility with the extraction path's
        # existing convention. "unset" keeps construction lazy so the failure
        # is a wrapped 401 on first use, not an import-time crash.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or settings.embedding_api_key or os.environ.get("OPENROUTER_API_KEY") or "unset",
            # Unlike OpenAI's, this base_url is not optional - the openai SDK
            # would otherwise talk to api.openai.com with an OpenRouter key.
            base_url=base_url or settings.embedding_base_url or settings.openrouter_base_url,
        )
        self._model = model
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)
        self._expected_dimensions = expected_dimensions or settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = await self._call_with_retry(texts)
        ensure_one_vector_per_text(vectors, texts, self._model)
        ensure_expected_dimensions(vectors, self._model, self._expected_dimensions)
        return vectors

    @openai_retry()
    async def _call_with_retry(self, texts: list[str]) -> list[list[float]]:
        async with self._semaphore:
            try:
                response = await self._client.embeddings.create(model=self._model, input=texts)
            except RETRYABLE_OPENAI_ERRORS:
                raise
            except openai.APIError as exc:
                message = f"embedding 생성 실패 ({len(texts)}개 텍스트): {exc}"
                if isinstance(exc, openai.AuthenticationError):
                    message = OPENROUTER_EMBEDDING_CREDENTIAL_HINT_KO
                raise EmbeddingError(message) from exc
        # OpenAI's own API contract guarantees `data` comes back in input
        # order, so OpenAIEmbeddingProvider maps it positionally. Here the
        # response is relayed by a gateway in front of some other vendor's
        # API, so that guarantee is second-hand - sort by the `index` field
        # (which the OpenAI schema requires each item to carry, and
        # OpenRouter does return) whenever it is actually present, and fall
        # back to positional order otherwise rather than crashing on a
        # provider that omits it. `embed()` then still catches a truncated
        # response via ensure_one_vector_per_text().
        data = list(response.data)
        if data and all(getattr(item, "index", None) is not None for item in data):
            data.sort(key=lambda item: item.index)
        return [item.embedding for item in data]
