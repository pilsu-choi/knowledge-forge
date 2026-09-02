"""Shared retry policy for LLM API calls - used by the Anthropic, OpenRouter,
and Claude Agent SDK provider implementations (Knowledge Extraction and
Vision extraction, for each) so backoff/attempt-count tuning doesn't drift
between them.
"""

import anthropic
import openai
from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ResultError
from tenacity import retry, retry_if_exception, retry_if_exception_type, stop_after_attempt, wait_exponential

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


# ResultError/CLIConnectionError carry no exception subtype per HTTP status
# (unlike anthropic.RateLimitError etc.) - the retryable condition has to be
# read off their attributes instead of matched by type, so this is a
# predicate (retry_if_exception) rather than a type tuple. CLINotFoundError
# (the `claude` binary is missing entirely) is a CLIConnectionError subclass
# but deliberately excluded - retrying it just repeats the same failure.
def is_retryable_claude_agent_sdk_error(exc: BaseException) -> bool:
    """Shared with claude_agent_sdk_extractor.py, which needs the same
    predicate outside the retry decorator: it must re-raise a retryable
    ResultError/CLIConnectionError as-is (so this predicate still matches it
    on the next tenacity attempt) but convert a non-retryable one into
    KnowledgeExtractionError - mirroring ClaudeExtractor's
    `except RETRYABLE_ANTHROPIC_ERRORS: raise` / `except anthropic.APIError`
    split, which a single type tuple can express but this attribute-based
    condition cannot.
    """
    if isinstance(exc, ResultError):
        return exc.api_error_status in (429, 500, 502, 503, 529) or exc.terminal_reason == "api_error"
    return isinstance(exc, CLIConnectionError) and not isinstance(exc, CLINotFoundError)


def _build_retry(retry_condition):
    return retry(
        retry=retry_condition,
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )


def anthropic_retry():
    return _build_retry(retry_if_exception_type(RETRYABLE_ANTHROPIC_ERRORS))


def openai_retry():
    return _build_retry(retry_if_exception_type(RETRYABLE_OPENAI_ERRORS))


def claude_agent_sdk_retry():
    return _build_retry(retry_if_exception(is_retryable_claude_agent_sdk_error))
