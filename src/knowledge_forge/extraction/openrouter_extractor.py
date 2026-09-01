"""Knowledge Extraction via OpenRouter (an OpenAI-Chat-Completions-compatible
gateway to many models, including Claude, GPT, Llama, etc.).

This is an alternative to ClaudeExtractor, not a replacement - the
Anthropic path stays the default/production path (see
knowledge_forge.extraction.factory). This exists so a future model can be
swapped in via `KF_LLM_PROVIDER=openrouter` + `KF_OPENROUTER_EXTRACTION_MODEL`
without touching pipeline code, since both classes implement the same
`Extractor` ABC.

The KnowledgeObject-building logic in `extract()` is intentionally
duplicated from ClaudeExtractor rather than shared via a base class - the
two providers' response shapes differ enough (parsed_output vs a raw JSON
string to validate) that a shared base would either leak provider-specific
details through the abstraction or need its own indirection layer. A few
duplicated lines here is a smaller risk than accidentally changing
ClaudeExtractor's already-tested behavior while trying to share code.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import openai

from knowledge_forge.config import settings
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.auth import OPENROUTER_CREDENTIAL_HINT_KO
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
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_TOKENS = 8000


def _to_strict_json_schema(schema: dict) -> dict:
    """OpenAI/OpenRouter structured-output "strict" mode requires every
    object node to set `additionalProperties: false` and list every one of
    its properties in `required` (truly-optional fields stay expressed as
    Pydantic already does, via a nullable `anyOf`, but must still be
    *listed* as required under strict mode) - Pydantic's own
    `model_json_schema()` does neither. Walks `$defs` and the schema body
    to patch every object node. Best-effort: this can't be verified against
    a live OpenRouter-routed model in this environment (no API key here),
    so it follows OpenAI's documented strict-mode shape but isn't
    guaranteed correct against every model OpenRouter might route to.
    """

    def _patch(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for value in node.values():
                _patch(value)
        elif isinstance(node, list):
            for item in node:
                _patch(item)

    schema = dict(schema)
    _patch(schema)
    return schema


class OpenRouterExtractor(Extractor):
    def __init__(
        self,
        client: openai.AsyncOpenAI | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        max_concurrency: int | None = None,
    ):
        # openai.AsyncOpenAI()'s own zero-arg credential resolution looks for
        # OPENAI_API_KEY, NOT OPENROUTER_API_KEY - it does not auto-read
        # OPENROUTER_API_KEY, so it must be passed explicitly here. Unlike
        # anthropic.AsyncAnthropic(), openai.AsyncOpenAI() raises immediately at
        # construction time if no api_key resolves at all (confirmed) -
        # fall back to a placeholder so construction stays as cheap/lazy as
        # the Anthropic path, and let the real 401 surface (and get wrapped
        # into KnowledgeExtractionError below) on the first actual call.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY") or "unset",
            base_url=base_url,
        )
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

    @openai_retry()
    async def _call_with_retry(self, chunk: Chunk, document: Document) -> ExtractionResult:
        async with self._semaphore:
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": render_user_prompt(chunk, document)},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "extraction_result",
                            "strict": True,
                            "schema": _to_strict_json_schema(ExtractionResult.model_json_schema()),
                        },
                    },
                )
            except RETRYABLE_OPENAI_ERRORS:
                raise
            except openai.APIError as exc:
                message = f"knowledge extraction failed for chunk {chunk.chunk_id}: {exc}"
                if isinstance(exc, openai.AuthenticationError):
                    message = f"{OPENROUTER_CREDENTIAL_HINT_KO} (chunk {chunk.chunk_id})"
                raise KnowledgeExtractionError(message, chunk_id=chunk.chunk_id) from exc
        content = response.choices[0].message.content
        return ExtractionResult.model_validate_json(content)
