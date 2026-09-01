"""Knowledge Extraction via the Anthropic Claude API.

Uses `client.messages.parse(..., output_format=ExtractionResult)`, the
SDK's structured-output helper - `ExtractionResult` (extraction.schemas)
doubles as the request schema and the validated response type, so there is
no separate JSON schema to hand-maintain.
"""

import logging
from datetime import datetime, timezone

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from knowledge_forge.exceptions import KnowledgeExtractionError
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

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8000

_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)


class ClaudeExtractor(Extractor):
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        # A bare Anthropic() resolves credentials itself (ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile) - no app-level
        # key management needed. Callers inject a client mainly for testing.
        self._client = client or anthropic.Anthropic()
        self._model = model
        self._max_tokens = max_tokens

    def extract(self, chunk: Chunk, document: Document, source: Source) -> KnowledgeObject:
        result = self._call_with_retry(chunk, document)
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

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _call_with_retry(self, chunk: Chunk, document: Document) -> ExtractionResult:
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": render_user_prompt(chunk, document)}],
                output_format=ExtractionResult,
            )
        except _RETRYABLE:
            raise
        except anthropic.APIError as exc:
            raise KnowledgeExtractionError(
                f"knowledge extraction failed for chunk {chunk.chunk_id}: {exc}",
                chunk_id=chunk.chunk_id,
            ) from exc
        return response.parsed_output
