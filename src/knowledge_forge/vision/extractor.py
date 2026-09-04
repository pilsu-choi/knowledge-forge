"""VisionExtractor: Claude-vision-based transcription of tables, figures,
charts, and diagrams cropped out of a document page.

This is the "hard/visual" half of the hybrid PDF/Image/PPTX parsing
design: plain running text is still extracted deterministically
(pdfplumber/python-pptx text APIs), but every table and every visual
figure/chart/diagram is rendered to an image and sent through this class
instead, per the user's explicit choice to always use VLM for those rather
than a rule-based-first-fallback-to-VLM policy.
"""

import asyncio
import base64
import logging
from typing import Any, cast

import anthropic
from anthropic.types import MessageParam
from anthropic.types.base64_image_source_param import Base64ImageSourceParam

from knowledge_forge.config import settings
from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.auth import CREDENTIAL_HINT_KO
from knowledge_forge.retry import RETRYABLE_ANTHROPIC_ERRORS, anthropic_retry
from knowledge_forge.vision.prompts import FIGURE_PROMPT, TABLE_PROMPT, TRANSCRIBE_PROMPT

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 4096


class VisionExtractor:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_concurrency: int | None = None,
    ):
        self._client = client or anthropic.AsyncAnthropic()
        self._model = model
        self._max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(max_concurrency or settings.max_concurrent_llm_requests)

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

    @anthropic_retry()
    async def _call(self, image_bytes: bytes, media_type: str, prompt: str) -> str:
        data = base64.standard_b64encode(image_bytes).decode("ascii")
        # `media_type` is plain `str` end-to-end (parsers hand back whatever
        # content_type the source file/library reports), but the SDK's param
        # type pins it to Anthropic's four supported image mime types - cast
        # rather than threading a Literal through every parser, since a mime
        # type outside that set is a genuine API-time failure either way.
        image_source: Base64ImageSourceParam = {
            "type": "base64",
            "media_type": cast(Any, media_type),
            "data": data,
        }
        messages: list[MessageParam] = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": image_source},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        async with self._semaphore:
            try:
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=messages,
                )
            except RETRYABLE_ANTHROPIC_ERRORS:
                raise
            except anthropic.APIError as exc:
                raise KnowledgeExtractionError(f"vision extraction failed: {exc}") from exc
            except TypeError as exc:
                if "Could not resolve authentication" in str(exc):
                    raise KnowledgeExtractionError(CREDENTIAL_HINT_KO) from exc
                raise
        text_blocks = [b.text for b in response.content if b.type == "text"]
        return "".join(text_blocks).strip()
