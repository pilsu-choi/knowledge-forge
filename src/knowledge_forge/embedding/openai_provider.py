"""Embedding generation via the OpenAI Embeddings API.

Modeled closely on extraction/openrouter_extractor.py's OpenRouterExtractor -
same lazy-credential-resolution / semaphore / retry-then-wrap shape, since
both are thin wrappers around an `openai.AsyncOpenAI` client. Unlike
OpenRouterExtractor, no `base_url` override is needed here: OpenRouter is a
gateway in front of many providers and models, but OpenAI's embeddings
endpoint IS the real OpenAI API, so the client's own default base_url is
already correct.
"""

import asyncio
import os

import openai

from knowledge_forge.config import settings
from knowledge_forge.embedding.base import EmbeddingProvider
from knowledge_forge.exceptions import EmbeddingError
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry

DEFAULT_MODEL = "text-embedding-3-small"

OPENAI_EMBEDDING_CREDENTIAL_HINT_KO = (
    "OpenAI API 자격 증명을 찾을 수 없습니다.\n"
    "  OPENAI_API_KEY 환경변수를 설정하세요 (https://platform.openai.com 에서 발급)."
)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        client: openai.AsyncOpenAI | None = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_concurrency: int | None = None,
    ):
        # Same lazy-credential trick as OpenRouterExtractor: openai.AsyncOpenAI()
        # raises immediately at construction time if no api_key resolves at
        # all, so fall back to a placeholder and let the real 401 surface (and
        # get wrapped into EmbeddingError below) on the first actual call.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY") or "unset",
        )
        self._model = model
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._call_with_retry(texts)

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
                    message = OPENAI_EMBEDDING_CREDENTIAL_HINT_KO
                raise EmbeddingError(message) from exc
        # response.data is guaranteed to be in the same order as `texts` per
        # the OpenAI Embeddings API contract, so a direct positional mapping
        # is safe (no need to sort/match by index).
        return [d.embedding for d in response.data]
