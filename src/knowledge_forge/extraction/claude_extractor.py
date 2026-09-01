"""Knowledge Extraction via the Anthropic Claude API.

Uses `client.messages.parse(..., output_format=ExtractionResult)`, the
SDK's structured-output helper - `ExtractionResult` (extraction.schemas)
doubles as the request schema and the validated response type, so there is
no separate JSON schema to hand-maintain.

One chunk bundle (see extraction/grouping.py) produces an `ExtractionOutcome`:
a list of typed KnowledgeObjects (one ENTITY object per extracted entity,
one object per typed knowledge item) plus the Relations connecting them -
see extraction/outcome.py for the (provider-agnostic) mapping and
extraction/base.py for why this replaced the old 1-chunk-1-object mapping.
"""

import asyncio
import logging

import anthropic

from knowledge_forge.config import settings
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.auth import CREDENTIAL_HINT_KO
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.extraction.outcome import build_extraction_outcome
from knowledge_forge.extraction.prompts import SYSTEM_PROMPT, render_user_prompt
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import Source
from knowledge_forge.retry import RETRYABLE_ANTHROPIC_ERRORS, anthropic_retry

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8000


class ClaudeExtractor(Extractor):
    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_concurrency: int | None = None,
    ):
        # A bare AsyncAnthropic() resolves credentials itself (ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile) - no app-level
        # key management needed. Callers inject a client mainly for testing.
        self._client = client or anthropic.AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)

    async def extract(self, chunks: list[Chunk], document: Document, source: Source) -> ExtractionOutcome:
        result = await self._call_with_retry(chunks, document)
        return build_extraction_outcome(result, chunks, document, source, model_used=self._model)

    @anthropic_retry()
    async def _call_with_retry(self, chunks: list[Chunk], document: Document) -> ExtractionResult:
        chunk_ids = ",".join(c.chunk_id for c in chunks)
        async with self._semaphore:
            try:
                response = await self._client.messages.parse(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": render_user_prompt(chunks, document)}],
                    output_format=ExtractionResult,
                )
            except RETRYABLE_ANTHROPIC_ERRORS:
                raise
            except anthropic.APIError as exc:
                raise KnowledgeExtractionError(
                    f"knowledge extraction failed for chunk(s) {chunk_ids}: {exc}",
                    chunk_id=chunk_ids,
                ) from exc
            except TypeError as exc:
                # The SDK raises a bare TypeError (not an APIError subclass) from
                # _build_headers when no credential resolves at request time -
                # see extraction.auth for the full resolution chain and the
                # CLI-level preflight check that normally catches this earlier.
                if "Could not resolve authentication" in str(exc):
                    raise KnowledgeExtractionError(
                        f"{CREDENTIAL_HINT_KO} (chunk(s) {chunk_ids})",
                        chunk_id=chunk_ids,
                    ) from exc
                raise
        return response.parsed_output
