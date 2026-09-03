"""Best-effort preflight credential check for the agent runtime - the same
role `extraction/auth.py` plays for Knowledge Extraction, deliberately not
shared with it (see `openrouter_extractor.py`'s module docstring on why a
little duplication beats a shared indirection layer here): `agent_provider`
and `llm_provider` are separate settings that can point at different
backends, so this has to key off `settings.agent_provider`, not
`settings.llm_provider`. WIF (Workload Identity Federation) env-var checks
are intentionally not duplicated here - an edge case `extraction/auth.py`
covers that agent sessions can pick up later if it turns out to matter.
"""

import os

from knowledge_forge.config import settings

_ANTHROPIC_PROFILE_DIR = os.path.expanduser("~/.config/anthropic")


def _has_anthropic_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return os.path.isdir(_ANTHROPIC_PROFILE_DIR) and bool(os.listdir(_ANTHROPIC_PROFILE_DIR))


def _has_openai_compatible_credentials() -> bool:
    # OPENROUTER_API_KEY covers the default base_url (OpenRouter);
    # OPENAI_API_KEY is accepted too since KF_AGENT_OPENAI_COMPATIBLE_BASE_URL
    # can be pointed at a plain OpenAI-compatible endpoint instead.
    return bool(os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def has_resolvable_agent_credentials() -> bool:
    if settings.agent_provider == "openai_compatible":
        return _has_openai_compatible_credentials()
    return _has_anthropic_credentials()


ANTHROPIC_AGENT_CREDENTIAL_HINT_KO = (
    "Anthropic API 자격 증명을 찾을 수 없습니다. 다음 중 하나를 설정하세요:\n"
    "  1) ANTHROPIC_API_KEY 환경변수\n"
    "  2) `ant auth login` (Claude Code OAuth 로그인)\n"
    "  3) ANTHROPIC_AUTH_TOKEN 환경변수"
)

OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO = (
    "OpenAI-호환 프로바이더(기본: OpenRouter) API 자격 증명을 찾을 수 없습니다.\n"
    "  OPENROUTER_API_KEY 또는 OPENAI_API_KEY 환경변수를 설정하세요."
)


def agent_credential_hint_ko() -> str:
    if settings.agent_provider == "openai_compatible":
        return OPENAI_COMPATIBLE_AGENT_CREDENTIAL_HINT_KO
    return ANTHROPIC_AGENT_CREDENTIAL_HINT_KO
