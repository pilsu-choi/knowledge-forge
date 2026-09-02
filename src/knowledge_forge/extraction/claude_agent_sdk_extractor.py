"""Knowledge Extraction via the Claude Agent SDK (`claude_agent_sdk`), which
drives the `claude` CLI as a subprocess instead of calling the Anthropic
Messages API directly (see claude_extractor.py for that path).

The point of this provider is authentication, not capability: the `claude`
CLI resolves its own credentials the same way interactive Claude Code does
- a browser OAuth login, a `claude setup-token` long-lived OAuth token
(CLAUDE_CODE_OAUTH_TOKEN, for headless/CI use), or a plain
ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN - so a machine that already has
Claude Code logged in needs no separate Anthropic API key to run
extraction. See extraction/auth.py for the credential preflight and
wiki/llm-providers.md for how to enable this via KF_LLM_PROVIDER.

Every built-in tool is disabled (`tools=[]`) and `max_turns=1`: this
provider is used for a single structured-output completion, the same
one-call-per-chunk-bundle shape as ClaudeExtractor/OpenRouterExtractor -
never an actual agentic/tool-using session. The CLI must not read/write
files or run commands on this call's behalf.
"""

import asyncio
import logging

from claude_agent_sdk import ClaudeAgentOptions, CLIConnectionError, ResultError, ResultMessage, query

from knowledge_forge.config import settings
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.auth import CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO
from knowledge_forge.extraction.base import ExtractionOutcome, Extractor
from knowledge_forge.extraction.outcome import build_extraction_outcome
from knowledge_forge.extraction.prompts import SYSTEM_PROMPT, render_user_prompt
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.models.chunk import Chunk
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import Source
from knowledge_forge.retry import claude_agent_sdk_retry, is_retryable_claude_agent_sdk_error

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"


class ClaudeAgentSDKExtractor(Extractor):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        cli_path: str | None = None,
        max_concurrency: int | None = None,
        query_fn=query,
    ):
        self._model = model
        self._cli_path = cli_path or settings.claude_agent_sdk_cli_path
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)
        # Injected for testing (a fake async generator) - defaults to the
        # real claude_agent_sdk.query().
        self._query_fn = query_fn

    async def extract(self, chunks: list[Chunk], document: Document, source: Source) -> ExtractionOutcome:
        result = await self._call_with_retry(chunks, document)
        return build_extraction_outcome(result, chunks, document, source, model_used=self._model)

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self._model,
            cli_path=self._cli_path,
            system_prompt=SYSTEM_PROMPT,
            tools=[],
            max_turns=1,
            output_format={"type": "json_schema", "schema": ExtractionResult.model_json_schema()},
        )

    @claude_agent_sdk_retry()
    async def _call_with_retry(self, chunks: list[Chunk], document: Document) -> ExtractionResult:
        chunk_ids = ",".join(c.chunk_id for c in chunks)
        async with self._semaphore:
            try:
                result_message = None
                async for message in self._query_fn(prompt=render_user_prompt(chunks, document), options=self._options()):
                    if isinstance(message, ResultMessage):
                        result_message = message
            except (CLIConnectionError, ResultError) as exc:
                # Re-raise retryable errors unconverted, same as
                # ClaudeExtractor's `except RETRYABLE_ANTHROPIC_ERRORS: raise`
                # - is_retryable_claude_agent_sdk_error (the tenacity retry
                # condition on this method) must still see the original type
                # on the next attempt, not a KnowledgeExtractionError.
                if is_retryable_claude_agent_sdk_error(exc):
                    raise
                if isinstance(exc, CLIConnectionError):
                    # Covers both "claude binary not found at all" and
                    # "spawned but couldn't connect" - both mean there's
                    # nothing for the CLI to authenticate with in the first
                    # place.
                    raise KnowledgeExtractionError(
                        f"{CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO} (chunk(s) {chunk_ids})",
                        chunk_id=chunk_ids,
                    ) from exc
                raise KnowledgeExtractionError(
                    f"knowledge extraction failed for chunk(s) {chunk_ids}: {exc}",
                    chunk_id=chunk_ids,
                ) from exc

        if result_message is None or result_message.subtype != "success" or result_message.structured_output is None:
            subtype = result_message.subtype if result_message else "no result message"
            raise KnowledgeExtractionError(
                f"knowledge extraction produced no structured output for chunk(s) {chunk_ids} (subtype: {subtype})",
                chunk_id=chunk_ids,
            )
        return ExtractionResult.model_validate(result_message.structured_output)
