"""Query Router - decides which retrieval source(s) (`file_search`/
`vector_search`) a given (sub-)query should actually be run against, instead
of always querying every configured source.

Deliberately a plain, dependency-free heuristic function, not an LLM call:
the agent's own tool-calling loop already has an LLM (the top-level model)
free to call `file_search`/`vector_search` directly whenever it wants - this
router only exists for `search/planner.py`'s sub-queries, which are generated
programmatically and have no model turn of their own to make that judgment
call. Spending an extra LLM round trip just to classify "is this an exact
lookup or a paraphrase" would cost more than the ambiguous cases it would
fix - a wrong guess here only means `plan_search` queries one extra source,
not a wrong final answer (the caller merges whatever comes back).

This intentionally does not attempt "real" intent classification/NLU (that's
Query Understanding in the broader RAG-architecture sense - out of scope,
see wiki/agent-search.md). It only distinguishes the one thing that actually
changes which of *this project's two* retrieval sources is likely to hit:
does the query carry an exact-match signal (a quoted phrase, an id-like
token) that `file_search`'s substring match is good at, or does it read as a
natural-language/conceptual question that `vector_search`'s embeddings are
good at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge_forge.search.constraints import _CODE_ID_RE, _QUOTED_RE

FILE_SEARCH = "file_search"
VECTOR_SEARCH = "vector_search"

# Quoted-phrase / code-id-like-token detection is shared with
# `search/constraints.py` rather than kept as a second, drifting copy here -
# reused as-is rather than duplicated (both cover the same "the user typed
# something literal" surface forms: `_QUOTED_RE` for quoted phrases,
# `_CODE_ID_RE` for "ko-abc123"/"PRJ-A"/"ERR_404"/"#1234"-shaped tokens).
# `_CODE_ID_RE`'s boundaries are ASCII-only lookaround
# (`(?<![\w가-힣])`/`(?![A-Za-z0-9_-])`), unlike a plain `\b` - Python's `\b`
# treats Hangul as a word character, so `\b...\b` never closes before a
# Korean particle ("ERR_404에서" has no `\w`/non-`\w` transition between "4"
# and "에", both count as `\w`) and the old copy of this regex here missed
# exactly that case (`has_exact_match_signal("ERR_404에서 뭐가 문제야")` was
# False).
# Document-name-shaped tokens the id regex above misses because they start
# with a digit or mix in Hangul: "2025_SH_업무_보고서" (3+ pieces joined by
# `_`/`-` - two pieces alone is too common in prose, e.g. a hyphenated
# English word), and anything carrying an office-document extension
# ("보고서.pdf"). Both are the user naming a specific file, which is a
# literal-match signal just like a quoted phrase (search/service.py's
# document_search is what actually resolves such a name to a document).
_JOINED_NAME_RE = re.compile(r"(?<![^\s(\[\"'「『])[^\s_\-]+(?:[_\-][^\s_\-]+){2,}(?![^\s)\]\"'」』.,?!])")
_FILE_EXTENSION_RE = re.compile(r"\S+\.(?:pdf|xlsx?|docx?|pptx?|hwpx?|csv|txt|md)(?![^\s)\]\"'」』,?!])", re.IGNORECASE)


def has_exact_match_signal(query: str) -> bool:
    """True when `query` carries something that should be matched literally
    - a quoted phrase, an id-like token, a joined document name or a file
    name with an extension. Exposed (rather than inlined in `route_query`)
    so a caller with more sources than file_search/vector_search can reuse
    the same judgment."""
    return bool(
        _QUOTED_RE.search(query)
        or _CODE_ID_RE.search(query)
        or _JOINED_NAME_RE.search(query)
        or _FILE_EXTENSION_RE.search(query)
    )
# Korean/English question words/markers - a reasonable proxy for "this reads
# as a natural-language question", which is exactly what embeddings are
# trained to match against, not literal substrings.
_QUESTION_WORD_RE = re.compile(r"(왜|어떻게|무엇|언제|누가|어디|얼마나|어떤|이유|원인|비교|요약|추천|why|how|what|when|who)")
_SHORT_QUERY_WORD_COUNT = 3
_LONG_QUERY_WORD_COUNT = 6


@dataclass
class RouteDecision:
    sources: list[str]
    reason: str


def route_query(query: str, available_sources: list[str]) -> RouteDecision:
    """`available_sources` is whatever the caller actually has wired up
    (`search/planner.py::plan_and_search` passes `["file_search"]` alone
    when no `PipelineStore` is configured) - this never recommends a source
    the caller doesn't have, and always returns at least one source (falling
    back to whatever is available) rather than an empty list.

    When both sources are available, every branch below runs both anyway -
    the only real decisions this function makes are the 0/1-source edge
    cases above. `sources`' order used to encode a "which source first"
    preference, but no caller ever read it (`search/planner.py::
    _execute_subquery` only checks `x in decision.sources`), so it was a
    distinction without a difference - dropped in favor of a fixed order and
    a `reason` that still says which heuristic fired, for logging/tests."""
    if not available_sources:
        raise ValueError("route_query() requires at least one available source")
    if len(available_sources) == 1:
        return RouteDecision(sources=list(available_sources), reason="유일하게 사용 가능한 소스")
    if VECTOR_SEARCH not in available_sources:
        return RouteDecision(sources=[FILE_SEARCH], reason="vector_search 미구성")

    stripped = query.strip()
    word_count = len(stripped.split())
    both = [FILE_SEARCH, VECTOR_SEARCH]

    if has_exact_match_signal(stripped):
        return RouteDecision(sources=both, reason="따옴표/ID/파일명 형태의 정확 일치 신호 - 두 소스 모두 병행")
    if word_count <= _SHORT_QUERY_WORD_COUNT:
        return RouteDecision(sources=both, reason="짧은 키워드성 질의 - 두 소스 모두 병행")
    if _QUESTION_WORD_RE.search(stripped) or stripped.endswith("?") or word_count > _LONG_QUERY_WORD_COUNT:
        return RouteDecision(sources=both, reason="자연어 질문 신호 - 두 소스 모두 병행")
    return RouteDecision(sources=both, reason="모호한 질의 - 두 소스 모두 병행(hybrid)")
