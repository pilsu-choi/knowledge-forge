"""Shared retry policy for LLM API calls - used by both the Anthropic and
OpenRouter provider implementations (Knowledge Extraction and Vision
extraction, for each) so backoff/attempt-count tuning doesn't drift
between them.
"""

import anthropic
import openai
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RETRYABLE_ANTHROPIC_ERRORS = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)

RETRYABLE_OPENAI_ERRORS = (
    openai.RateLimitError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


def _build_retry(retryable_errors):
    return retry(
        retry=retry_if_exception_type(retryable_errors),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )


def anthropic_retry():
    return _build_retry(RETRYABLE_ANTHROPIC_ERRORS)


def openai_retry():
    return _build_retry(RETRYABLE_OPENAI_ERRORS)
