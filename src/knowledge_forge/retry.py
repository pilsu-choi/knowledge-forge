"""Shared retry policy for Anthropic API calls - used by both Knowledge
Extraction (extraction.claude_extractor) and Vision extraction (vision.extractor)
so the two don't drift on backoff/attempt-count tuning.
"""

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

RETRYABLE_ANTHROPIC_ERRORS = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
)


def anthropic_retry():
    return retry(
        retry=retry_if_exception_type(RETRYABLE_ANTHROPIC_ERRORS),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
