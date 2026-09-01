"""Knowledge Extraction via the Anthropic Claude API.

Uses `client.messages.parse(..., output_format=ExtractionResult)`, the
SDK's structured-output helper - `ExtractionResult` (extraction.schemas)
doubles as the request schema and the validated response type, so there is
no separate JSON schema to hand-maintain.
"""

import asyncio
import logging
from datetime import datetime, timezone

import anthropic

from knowledge_forge.config import settings
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.auth import CREDENTIAL_HINT_KO
from knowledge_forge.extraction.base import Extractor
from knowledge_forge.extraction.prompts import SYSTEM_PROMPT, render_user_prompt
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.ids import stable_id
from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import (
    ChunkRef,
    DocumentRef,
    Entity,
    KnowledgeObject,
    Relation,
)
from knowledge_forge.models.source import Source, SourceRef
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

    async def extract(self, chunk: Chunk, document: Document, source: Source) -> KnowledgeObject:
        result = await self._call_with_retry(chunk, document)
        return KnowledgeObject(
            knowledge_object_id=stable_id(chunk.chunk_id, "ko"),
            source=SourceRef.from_source(source),
            document=DocumentRef(document_id=document.document_id, title=document.metadata.title),
            chunk=ChunkRef(
                chunk_id=chunk.chunk_id,
                hierarchy_level=chunk.hierarchy_level,
                sequence_index=chunk.sequence_index,
            ),
            summary=result.summary,
            facts=result.facts,
            entities=[Entity(**e.model_dump()) for e in result.entities],
            relations=[Relation(**r.model_dump()) for r in result.relations],
            topics=result.topics,
            document_type=result.document_type,
            extracted_at=datetime.now(timezone.utc),
            model_used=self._model,
        )

    @anthropic_retry()
    async def _call_with_retry(self, chunk: Chunk, document: Document) -> ExtractionResult:
        async with self._semaphore:
            try:
                response = await self._client.messages.parse(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": render_user_prompt(chunk, document)}],
                    output_format=ExtractionResult,
                )
            except RETRYABLE_ANTHROPIC_ERRORS:
                raise
            except anthropic.APIError as exc:
                raise KnowledgeExtractionError(
                    f"knowledge extraction failed for chunk {chunk.chunk_id}: {exc}",
                    chunk_id=chunk.chunk_id,
                ) from exc
            except TypeError as exc:
                # The SDK raises a bare TypeError (not an APIError subclass) from
                # _build_headers when no credential resolves at request time -
                # see extraction.auth for the full resolution chain and the
                # CLI-level preflight check that normally catches this earlier.
                if "Could not resolve authentication" in str(exc):
                    raise KnowledgeExtractionError(
                        f"{CREDENTIAL_HINT_KO} (chunk {chunk.chunk_id})",
                        chunk_id=chunk.chunk_id,
                    ) from exc
                raise
        return response.parsed_output
