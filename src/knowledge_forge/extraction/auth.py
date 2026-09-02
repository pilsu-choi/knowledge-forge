"""Best-effort preflight check for LLM provider credentials.

All three provider SDKs resolve credentials lazily, at request time - the
Anthropic SDK in this order: ANTHROPIC_API_KEY -> ANTHROPIC_AUTH_TOKEN ->
an `ant auth login` OAuth profile -> Workload Identity Federation env vars
-> the default profile on disk (`ClaudeExtractor` relies on that by
constructing a bare `anthropic.AsyncAnthropic()`); OpenRouter via a single
OPENROUTER_API_KEY env var; the Claude Agent SDK by spawning the `claude`
CLI as a subprocess and letting *it* resolve credentials the same way
interactive Claude Code does (browser OAuth login, a `claude setup-token`
CLAUDE_CODE_OAUTH_TOKEN, or ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN) - see
claude_agent_sdk_extractor.py. This module does NOT replace that
resolution logic (only the SDK/CLI is authoritative on whether a call will
actually succeed). It exists so the CLI can fail fast with a clear Korean
message *before* running a whole pipeline, instead of a raw error surfacing
deep inside the first extraction call (see claude_extractor.py's/
openrouter_extractor.py's/claude_agent_sdk_extractor.py's own catch of that
failure mode as the second line of defense for whatever this preflight
check misses).
"""

import os
import shutil

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


def _has_claude_agent_sdk_credentials() -> bool:
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    # The `claude` CLI resolves its own login state (browser OAuth,
    # `claude setup-token`, or an API key) independently once spawned - this
    # only confirms the binary is on PATH to spawn at all (or, if
    # KF_CLAUDE_AGENT_SDK_CLI_PATH points elsewhere, that that path exists).
    # It does NOT confirm the CLI is actually logged in; that can only be
    # known by actually invoking it, which this preflight intentionally
    # avoids (same limitation as the `~/.config/anthropic` profile-dir check
    # above).
    cli_path = settings.claude_agent_sdk_cli_path
    if cli_path:
        return os.path.isfile(cli_path) and os.access(cli_path, os.X_OK)
    return shutil.which("claude") is not None


def has_resolvable_credentials() -> bool:
    if settings.llm_provider == "openrouter":
        return _has_openrouter_credentials()
    if settings.llm_provider == "claude_agent_sdk":
        return _has_claude_agent_sdk_credentials()
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

CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO = (
    "Claude Agent SDK가 사용할 `claude` CLI를 찾거나 인증할 수 없습니다. 다음 중 하나를 준비하세요:\n"
    "  1) `claude` CLI를 설치하고 로그인하세요 (`claude` 실행 후 브라우저 OAuth 로그인)\n"
    "  2) CLAUDE_CODE_OAUTH_TOKEN 환경변수 (`claude setup-token`으로 발급 - headless/CI용)\n"
    "  3) ANTHROPIC_API_KEY 또는 ANTHROPIC_AUTH_TOKEN 환경변수\n"
    "  CLI가 PATH에 없다면 KF_CLAUDE_AGENT_SDK_CLI_PATH로 경로를 지정하세요."
)


def credential_hint_ko() -> str:
    """Provider-aware preflight message - used by cli.py's top-level check
    so it never tells an OpenRouter/Claude Agent SDK user to set
    ANTHROPIC_API_KEY (or vice versa)."""
    if settings.llm_provider == "openrouter":
        return OPENROUTER_CREDENTIAL_HINT_KO
    if settings.llm_provider == "claude_agent_sdk":
        return CLAUDE_AGENT_SDK_CREDENTIAL_HINT_KO
    return CREDENTIAL_HINT_KO
