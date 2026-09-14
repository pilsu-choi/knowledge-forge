"""Groups a document's chunks into the bundles Extractor.extract() consumes
(wiki/roadmap.md 5단계, item 1).

A single typed KnowledgeObject (e.g. a Decision) can span more than one
adjacent chunk - the chunk that states the decision and the next one that
states its reason. `KnowledgeObject.evidence` has always been a list for
this reason (see models/knowledge_object.py), but until this module
existed, `Extractor.extract()` only ever saw one chunk at a time, so
evidence was never more than a single-element list in practice.

Grouping is a plain windowing pass over one document's already-ordered
chunk list (chunker.chunk() output), not a semantic/similarity pass -
adjacency in the chunk sequence is the cheap, reliable proxy for "likely
part of the same claim". It's token-budget based (see
config.extraction_group_max_tokens), not count based: one already-large
chunk (e.g. a whole Chapter under AdaptiveChunker's budget) stays alone in
its own call, while several small chunks (short Slack messages, merged
paragraph leaves - see chunking/adaptive.py's small-sibling merge) get
bundled into one call up to the token budget, which is what actually cuts
the number of LLM calls a token-count knob wouldn't: two documents with the
same chunk *count* can need a very different number of extraction calls
depending on how large those chunks are.
"""

from knowledge_forge.models.chunk import Chunk

DEFAULT_GROUP_MAX_TOKENS = 4000


def group_chunks(chunks: list[Chunk], max_tokens: int = DEFAULT_GROUP_MAX_TOKENS) -> list[list[Chunk]]:
    """Greedily bundles consecutive chunks (preserving order) so each
    bundle's total `token_count` stays within `max_tokens` - a single chunk
    already at or over the budget is never split further, it just forms a
    bundle of one."""
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")
    groups: list[list[Chunk]] = []
    current: list[Chunk] = []
    current_tokens = 0
    for chunk in chunks:
        if current and current_tokens + chunk.token_count > max_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append(chunk)
        current_tokens += chunk.token_count
    if current:
        groups.append(current)
    return groups
