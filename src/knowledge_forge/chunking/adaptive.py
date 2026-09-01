"""The Adaptive Chunker: structure-based chunking that preserves the
largest possible semantic unit (PRD section 3-3).

Walks Document -> Chapter -> Section -> Paragraph -> Sentence -> Line and
only descends to a finer level when the current unit's text exceeds the
token budget. No sliding-window overlap between emitted chunks - that is
deliberate: the point of structure-based chunking is non-overlapping
semantic units, not a token-count sliding window.
"""

import logging
from dataclasses import dataclass, field

from knowledge_forge.chunking.base import Chunker
from knowledge_forge.chunking.hierarchy import Node, build_tree, split_table_by_rows, split_to_next_level
from knowledge_forge.chunking.tokenizer import count_tokens, decode, encode
from knowledge_forge.ids import stable_id
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document

logger = logging.getLogger(__name__)

# tiktoken's cl100k_base is a heuristic stand-in for Claude's actual
# tokenizer (see chunking.tokenizer), so the real split threshold is a
# fraction of the nominal budget - this absorbs count drift without
# risking an emitted chunk blowing the real context window.
DEFAULT_SAFETY_MARGIN = 0.8


@dataclass
class RenderedChunk:
    level: HierarchyLevel
    text: str
    element_ids: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)


class AdaptiveChunker(Chunker):
    def __init__(self, max_tokens_per_chunk: int = 4000, safety_margin: float = DEFAULT_SAFETY_MARGIN):
        self._max_tokens = max_tokens_per_chunk
        self._budget = max(1, int(max_tokens_per_chunk * safety_margin))

    def chunk(self, document: Document) -> list[Chunk]:
        root = build_tree(document)
        rendered = self._chunk_node(root)

        chunks: list[Chunk] = []
        for index, item in enumerate(rendered):
            page_range = (min(item.pages), max(item.pages)) if item.pages else None
            chunks.append(
                Chunk(
                    chunk_id=stable_id(document.document_id, str(index), item.level.value),
                    document_id=document.document_id,
                    source_id=document.source.source_id,
                    hierarchy_level=item.level,
                    sequence_index=index,
                    # Only leaf units are materialized as Chunks in this
                    # algorithm - an ancestor that got split was never
                    # itself emitted, so there's no persisted parent Chunk
                    # to point at yet. Reserved for a future multi-resolution
                    # chunk store.
                    parent_chunk_id=None,
                    text=item.text,
                    token_count=count_tokens(item.text),
                    element_ids=item.element_ids,
                    page_range=page_range,
                )
            )
        return chunks

    def _chunk_node(self, node: Node) -> list[RenderedChunk]:
        if not node.text:
            return []
        if count_tokens(node.text) <= self._budget:
            # Largest semantic unit that still fits the budget - keep it whole.
            return [RenderedChunk(level=node.level, text=node.text, element_ids=node.element_ids, pages=node.pages)]

        if node.children is not None:
            children = node.children
        elif node.table_rows is not None:
            # Tabular data: split by row (header repeated) rather than the
            # generic sentence/line split, which would cut mid-row and lose
            # the header. Falls through to the generic path if row-splitting
            # can't make progress (e.g. a single oversized row).
            children = split_table_by_rows(node, self._budget) or split_to_next_level(node)
        else:
            children = split_to_next_level(node)
        if not children:
            return self._hard_split(node)

        result: list[RenderedChunk] = []
        for child in children:
            result.extend(self._chunk_node(child))
        return result

    def _hard_split(self, node: Node) -> list[RenderedChunk]:
        """Last resort: a unit that still exceeds budget with no further
        structural boundary to split on (e.g. one giant sentence/line with
        no whitespace). Falls back to a raw token-count split.
        """
        logger.warning(
            "adaptive chunker: no structural boundary left below %s tokens for a %s-level "
            "unit (%d tokens) - falling back to a raw token split",
            self._budget,
            node.level.value,
            count_tokens(node.text),
        )
        tokens = encode(node.text)
        pieces = [decode(tokens[i : i + self._budget]) for i in range(0, len(tokens), self._budget)]
        return [
            RenderedChunk(level=HierarchyLevel.LINE, text=piece, element_ids=node.element_ids, pages=node.pages)
            for piece in pieces
        ]
