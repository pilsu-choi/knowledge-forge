"""Best-effort preflight check for LLM provider credentials.

Both provider SDKs (anthropic, openai-pointed-at-OpenRouter) resolve
credentials lazily, at request time - this module does NOT replace that
resolution logic (only the SDK is authoritative on whether a call will
actually succeed). It exists so the CLI can fail fast with a clear Korean
message *before* running a whole pipeline, instead of a raw error surfacing
deep inside the first extraction call (see claude_extractor.py's/
openrouter_extractor.py's own catch of that failure mode as the second
line of defense for whatever this preflight check misses).
"""

import os

from knowledge_forge.config import settings

_WIF_VARS = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
)
_WIF_TOKEN_VARS = ("ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_IDENTITY_TOKEN")
_DEFAULT_PROFILE_DIR = os.path.expanduser("~/.config/anthropic")


def _has_anthropic_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if all(os.environ.get(v) for v in _WIF_VARS) and any(os.environ.get(v) for v in _WIF_TOKEN_VARS):
        return True
    # `ant auth login` (Claude Code OAuth login) persists a profile here;
    # its presence doesn't guarantee validity, but its absence combined
    # with no env vars set means the SDK has nothing to resolve at all.
    if os.path.isdir(_DEFAULT_PROFILE_DIR) and os.listdir(_DEFAULT_PROFILE_DIR):
        return True
    return False


def _has_openrouter_credentials() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def has_resolvable_credentials() -> bool:
    if settings.llm_provider == "openrouter":
        return _has_openrouter_credentials()
    return _has_anthropic_credentials()


# Fixed Anthropic-specific hint - only ever surfaced on the Anthropic-only
# error paths inside claude_extractor.py/vision/extractor.py, which only
# run when the Anthropic provider is active in the first place.
CREDENTIAL_HINT_KO = (
    "Anthropic API 자격 증명을 찾을 수 없습니다. 다음 중 하나를 설정하세요:\n"
    "  1) ANTHROPIC_API_KEY 환경변수\n"
    "  2) `ant auth login` (Claude Code OAuth 로그인)\n"
    "  3) ANTHROPIC_AUTH_TOKEN 환경변수"
)

OPENROUTER_CREDENTIAL_HINT_KO = (
    "OpenRouter API 자격 증명을 찾을 수 없습니다.\n"
    "  OPENROUTER_API_KEY 환경변수를 설정하세요 (https://openrouter.ai 에서 발급)."
)


def credential_hint_ko() -> str:
    """Provider-aware preflight message - used by cli.py's top-level check
    so it never tells an OpenRouter user to set ANTHROPIC_API_KEY (or
    vice versa)."""
    if settings.llm_provider == "openrouter":
        return OPENROUTER_CREDENTIAL_HINT_KO
    return CREDENTIAL_HINT_KO
