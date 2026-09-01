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
part of the same claim". `group_size=1` (the default, see
config.extraction_chunk_group_size) reproduces the exact prior
one-chunk-per-call behavior.
"""

from knowledge_forge.models.chunk import Chunk


def group_chunks(chunks: list[Chunk], group_size: int = 1) -> list[list[Chunk]]:
    """Splits `chunks` into consecutive, non-overlapping windows of up to
    `group_size` chunks each, preserving order. The last window may be
    smaller than `group_size` if it doesn't divide evenly."""
    if group_size < 1:
        raise ValueError("group_size must be >= 1")
    return [chunks[i : i + group_size] for i in range(0, len(chunks), group_size)]
