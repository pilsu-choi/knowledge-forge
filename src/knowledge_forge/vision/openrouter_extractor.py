"""Vision extraction via OpenRouter - the same three-method surface as
VisionExtractor (vision.extractor), backed by OpenAI-compatible
Chat Completions vision content blocks instead of Anthropic's native
`image`/`source` block shape. An alternative provider, not a replacement -
see knowledge_forge.vision.factory for the KF_LLM_PROVIDER switch.
"""

import base64
import logging
import os

import openai

from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.extraction.auth import OPENROUTER_CREDENTIAL_HINT_KO
from knowledge_forge.retry import RETRYABLE_OPENAI_ERRORS, openai_retry
from knowledge_forge.vision.prompts import FIGURE_PROMPT, TABLE_PROMPT, TRANSCRIBE_PROMPT

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MAX_TOKENS = 4096


class OpenRouterVisionExtractor:
    def __init__(
        self,
        client: openai.OpenAI | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
    ):
        # See extraction/openrouter_extractor.py for why the "unset"
        # placeholder is needed - openai.OpenAI() raises at construction
        # (not lazily, unlike anthropic.Anthropic()) with no api_key at all.
        self._client = client or openai.OpenAI(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY") or "unset",
            base_url=base_url,
        )
        self._model = model
        self._max_tokens = max_tokens

    def extract_table_markdown(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        """Returns a GFM markdown table transcription of the cropped region."""
        return self._call(image_bytes, media_type, TABLE_PROMPT)

    def describe_figure(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        """Returns a descriptive paragraph for a cropped figure/chart/diagram."""
        return self._call(image_bytes, media_type, FIGURE_PROMPT)

    def transcribe_image(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        """Returns a transcription/description for a standalone image file
        (not a crop of a larger page)."""
        return self._call(image_bytes, media_type, TRANSCRIBE_PROMPT)

    @openai_retry()
    def _call(self, image_bytes: bytes, media_type: str, prompt: str) -> str:
        data = base64.standard_b64encode(image_bytes).decode("ascii")
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
        except RETRYABLE_OPENAI_ERRORS:
            raise
        except openai.APIError as exc:
            message = f"vision extraction failed: {exc}"
            if isinstance(exc, openai.AuthenticationError):
                message = OPENROUTER_CREDENTIAL_HINT_KO
            raise KnowledgeExtractionError(message) from exc
        return (response.choices[0].message.content or "").strip()
