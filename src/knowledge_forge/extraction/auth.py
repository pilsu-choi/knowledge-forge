"""Best-effort preflight check for Anthropic credentials.

The Anthropic SDK itself resolves credentials lazily, at request time, in
this order: ANTHROPIC_API_KEY -> ANTHROPIC_AUTH_TOKEN -> an `ant auth login`
OAuth profile -> Workload Identity Federation env vars -> the default
profile on disk. `ClaudeExtractor` already relies on that by constructing
a bare `anthropic.Anthropic()` - this module does NOT replace that
resolution logic (only the SDK is authoritative on whether a call will
actually succeed). It exists so the CLI can fail fast with a clear Korean
message *before* running a whole pipeline, instead of the raw `TypeError`
the SDK raises deep inside `_build_headers` on the first extraction call
(see claude_extractor.py's own catch of that error as the second line of
defense for whatever this preflight check misses).
"""

import os

_WIF_VARS = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
)
_WIF_TOKEN_VARS = ("ANTHROPIC_IDENTITY_TOKEN_FILE", "ANTHROPIC_IDENTITY_TOKEN")
_DEFAULT_PROFILE_DIR = os.path.expanduser("~/.config/anthropic")


def has_resolvable_credentials() -> bool:
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


CREDENTIAL_HINT_KO = (
    "Anthropic API 자격 증명을 찾을 수 없습니다. 다음 중 하나를 설정하세요:\n"
    "  1) ANTHROPIC_API_KEY 환경변수\n"
    "  2) `ant auth login` (Claude Code OAuth 로그인)\n"
    "  3) ANTHROPIC_AUTH_TOKEN 환경변수"
)
