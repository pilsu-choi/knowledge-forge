"""Vision extraction (table/figure/image transcription) via the Claude
Agent SDK (`claude_agent_sdk`) - the vision counterpart of
`extraction/claude_agent_sdk_extractor.py`, which that module's docstring
says isn't implemented yet ("Vision 추출은 아직 이 프로바이더로 구현되어
있지 않다"). Same point as that module: authentication, not capability - a
machine with Claude Code already logged in (OAuth / CLAUDE_CODE_OAUTH_TOKEN)
needs no separate ANTHROPIC_API_KEY to run VLM table/figure/image
extraction either.

## Why this needs streaming input, unlike the extraction provider

`extraction/claude_agent_sdk_extractor.py` calls `query(prompt=<str>, ...)`
- a plain string prompt. `_internal/client.py`'s handling of a string
prompt hardcodes `message.content` to that bare string
(`{"role": "user", "content": prompt}`), so there is no way to attach an
image content block through it.

`query()` also accepts `prompt: AsyncIterable[dict]` ("streaming input
mode" - see `claude_agent_sdk/query.py`'s docstring). Each dict is written
to the CLI's stdin as-is via `json.dumps(message)`
(`_internal/query.py::stream_input`, no validation of `message["content"]`'s
shape) - so a streamed message's `content` can be a list of content blocks
instead of a bare string, exactly like `client.messages.create()`'s
`messages` param. Anthropic's native image block shape
(`{"type": "image", "source": {"type": "base64", "media_type": ..., "data":
...}}`) survives this path: the CLI's stream-json protocol is a thin
wrapper over the Messages API, and the SDK forwards `message.content`
unvalidated (confirmed by reading `_internal/query.py::stream_input` and
`_internal/client.py::_process_query_inner` in the installed
claude_agent_sdk 0.2.151 - see wiki/vision-extraction.md for the finding
written up in one place). So this extractor sends a one-item async
generator yielding a single user message whose `content` is
`[image block, text block]`, instead of a plain string.

No `output_format` schema here (unlike the extraction provider) - vision
wants free-form markdown/text, not structured JSON, so the plain
`ResultMessage.result` string is used instead of `.structured_output`.
"""

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, CLIConnectionError, ResultError, ResultMessage, query

from knowledge_forge.config import settings
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.auth import CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO
from knowledge_forge.retry import claude_agent_sdk_retry, is_retryable_claude_agent_sdk_error
from knowledge_forge.vision.prompts import FIGURE_PROMPT, TABLE_PROMPT, TRANSCRIBE_PROMPT

logger = logging.getLogger(__name__)

# Same default as extraction/claude_agent_sdk_extractor.py - a cheaper model
# than VisionExtractor's claude-opus-5 default, but this provider's whole
# point is "runs with whatever the logged-in `claude` CLI can use", not cost
# tuning (see wiki/llm-providers.md's cost lever guidance for that).
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class ClaudeAgentSDKVisionExtractor:
    """Same three-method public surface as `VisionExtractor`/
    `OpenRouterVisionExtractor` (`vision/factory.py::VisionExtractorLike`) -
    parsers call these without knowing which provider backs them."""

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

    async def extract_table_markdown(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        """Returns a GFM markdown table transcription of the cropped region."""
        return await self._call(image_bytes, media_type, TABLE_PROMPT)

    async def describe_figure(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        """Returns a descriptive paragraph for a cropped figure/chart/diagram."""
        return await self._call(image_bytes, media_type, FIGURE_PROMPT)

    async def transcribe_image(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        """Returns a transcription/description for a standalone image file
        (not a crop of a larger page)."""
        return await self._call(image_bytes, media_type, TRANSCRIBE_PROMPT)

    def _options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self._model,
            cli_path=self._cli_path,
            tools=[],
            # 1 is enough here (unlike the extraction provider's 3): there is
            # no output_format schema to re-validate against, just free text.
            max_turns=1,
        )

    @staticmethod
    async def _prompt_stream(image_bytes: bytes, media_type: str, prompt: str) -> AsyncIterator[dict[str, Any]]:
        data = base64.standard_b64encode(image_bytes).decode("ascii")
        yield {
            "type": "user",
            "session_id": "",
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": prompt},
                ],
            },
        }

    @claude_agent_sdk_retry()
    async def _call(self, image_bytes: bytes, media_type: str, prompt: str) -> str:
        async with self._semaphore:
            try:
                result_message = None
                async for message in self._query_fn(
                    prompt=self._prompt_stream(image_bytes, media_type, prompt), options=self._options()
                ):
                    if isinstance(message, ResultMessage):
                        result_message = message
            except (CLIConnectionError, ResultError) as exc:
                # Same split as extraction/claude_agent_sdk_extractor.py: a
                # retryable error is re-raised unconverted so the tenacity
                # retry condition still recognizes it on the next attempt.
                if is_retryable_claude_agent_sdk_error(exc):
                    raise
                if isinstance(exc, CLIConnectionError):
                    raise KnowledgeExtractionError(CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO) from exc
                raise KnowledgeExtractionError(f"vision extraction failed: {exc}") from exc

        if result_message is None or result_message.subtype != "success" or not result_message.result:
            subtype = result_message.subtype if result_message else "no result message"
            raise KnowledgeExtractionError(f"vision extraction produced no text output (subtype: {subtype})")
        return result_message.result.strip()
