"""Cheap, dependency-free text similarity - the shared primitive behind
dedup.py (same-run near-duplicate merging) and extraction/versioning.py
(matching a re-extraction against its prior version). Both problems reduce
to "are these two KnowledgeObjects' texts close enough to be the same
underlying thing", so the comparison itself lives here once.

Uses difflib.SequenceMatcher rather than embeddings on purpose - this stays
a character-level diff tool, not a semantic-similarity one, even though
embeddings are no longer out of scope (see wiki/embeddings.md: a
KnowledgeObject's summary/content/topics are now embedded into pgvector's
`knowledge_object_embeddings`). The two solve different problems: dedup/
supersedes need "is this near-identical text, from a re-run or an adjacent
chunk" (character-level ratio is a fast, model-free proxy for that, and a
KnowledgeObject's `summary`/`content.statement` is short enough - one
sentence/claim - for it to work well), while embeddings target "is this
semantically related text" for meaning-based search (still design-only past
generation - see wiki/agent-search.md). Swapping this module's comparison
for an embedding-based one would change what dedup/supersedes actually
matches (paraphrases would start merging, not just near-identical text),
which isn't the goal here.
"""

from difflib import SequenceMatcher


def text_similarity(a: str, b: str) -> float:
    """Symmetric similarity in [0, 1]; 1.0 means identical text."""
    a_norm = " ".join(a.strip().lower().split())
    b_norm = " ".join(b.strip().lower().split())
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()
