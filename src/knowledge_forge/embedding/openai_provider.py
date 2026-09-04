"""Embedding generation via the OpenAI Embeddings API.

Modeled closely on extraction/openrouter_extractor.py's OpenRouterExtractor -
same lazy-credential-resolution / semaphore / retry-then-wrap shape, since
both are thin wrappers around an `openai.AsyncOpenAI` client. The default
base_url is the client's own (OpenAI's embeddings endpoint IS the real
OpenAI API, unlike OpenRouter which is a gateway and must be pointed at
explicitly), but `KF_EMBEDDING_BASE_URL` can override it - that is what
makes an OpenAI-compatible self-hosted endpoint (vLLM, TEI, LM Studio)
usable through this class without a third provider implementation.
"""

import asyncio
import os

import openai

from knowledge_forge.config import settings
from knowledge_forge.embedding.base import EmbeddingProvider
from knowledge_forge.embedding.validation import ensure_expected_dimensions, ensure_one_vector_per_text
from knowledge_forge.exceptions import EmbeddingError
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry

DEFAULT_MODEL = "text-embedding-3-small"

OPENAI_EMBEDDING_CREDENTIAL_HINT_KO = (
    "OpenAI API 자격 증명을 찾을 수 없습니다.\n"
    "  .env에 KF_EMBEDDING_API_KEY=sk-... 를 적거나 OPENAI_API_KEY 환경변수를 설정하세요"
    " (https://platform.openai.com 에서 발급).\n"
    "  주의: .env의 OPENAI_API_KEY는 KF_ 접두가 아니라서 읽히지 않습니다 - 자세한 이유는"
    " config.py의 Settings docstring 참고."
)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        client: openai.AsyncOpenAI | None = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_concurrency: int | None = None,
        base_url: str | None = None,
        expected_dimensions: int | None = None,
    ):
        # Same lazy-credential trick as OpenRouterExtractor: openai.AsyncOpenAI()
        # raises immediately at construction time if no api_key resolves at
        # all, so fall back to a placeholder and let the real 401 surface (and
        # get wrapped into EmbeddingError below) on the first actual call.
        #
        # The `settings.embedding_api_key` step in the middle is what makes
        # the key settable from `.env` at all - pydantic-settings reads .env
        # into Settings but does NOT export it to os.environ, so a bare
        # `OPENAI_API_KEY=` line in .env never reaches the os.environ.get()
        # below. See config.py's Settings docstring. The os.environ fallback
        # is kept last so an already-exported OPENAI_API_KEY keeps working
        # exactly as before.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or settings.embedding_api_key or os.environ.get("OPENAI_API_KEY") or "unset",
            # None means "the SDK's own default" (api.openai.com) - only an
            # explicit override changes it.
            base_url=base_url or settings.embedding_base_url,
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
                    message = OPENAI_EMBEDDING_CREDENTIAL_HINT_KO
                raise EmbeddingError(message) from exc
        # response.data is guaranteed to be in the same order as `texts` per
        # the OpenAI Embeddings API contract, so a direct positional mapping
        # is safe (no need to sort/match by index).
        return [d.embedding for d in response.data]
