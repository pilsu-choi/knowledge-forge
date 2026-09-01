"""Cheap, dependency-free text similarity - the shared primitive behind
dedup.py (same-run near-duplicate merging) and extraction/versioning.py
(matching a re-extraction against its prior version). Both problems reduce
to "are these two KnowledgeObjects' texts close enough to be the same
underlying thing", so the comparison itself lives here once.

Uses difflib.SequenceMatcher rather than embeddings/a vector index on
purpose - Vector DB is explicitly out of scope (see wiki/roadmap.md's
excluded-scope table), and a KnowledgeObject's `summary`/`content.statement`
is short enough (one sentence/claim) that character-level ratio is a
reasonable proxy for near-duplication without a model call.
"""

from difflib import SequenceMatcher


def text_similarity(a: str, b: str) -> float:
    """Symmetric similarity in [0, 1]; 1.0 means identical text."""
    a_norm = " ".join(a.strip().lower().split())
    b_norm = " ".join(b.strip().lower().split())
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()
