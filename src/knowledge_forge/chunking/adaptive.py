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
from knowledge_forge.chunking.hierarchy import Node, build_tree, level_rank, split_table_by_rows, split_to_next_level
from knowledge_forge.chunking.tokenizer import count_tokens, decode, encode
from knowledge_forge.ids import stable_id
from knowledge_forge.models.chunk import Chunk, HierarchyLevel
from knowledge_forge.models.document import Document
from knowledge_forge.text_normalize import to_nfc

logger = logging.getLogger(__name__)

# tiktoken's cl100k_base is a heuristic stand-in for Claude's actual
# tokenizer (see chunking.tokenizer), so the real split threshold is a
# fraction of the nominal budget - this absorbs count drift without
# risking an emitted chunk blowing the real context window.
DEFAULT_SAFETY_MARGIN = 0.8

# Below this many tokens, a leaf chunk (a lone bullet, a title-only slide
# paragraph) is considered "too small to stand alone" and is a candidate for
# the bottom-up sibling merge in _merge_small_siblings(). Chosen well under
# a typical chunk budget so merging never fights the split pass - it only
# mops up fragments the split pass had no reason to keep separate.
DEFAULT_MIN_TOKENS = 50

# Levels at which _chunk_node() runs the sibling-merge pass over its
# children's results: a Section's own Paragraph-level children (one per
# source DocumentElement - see hierarchy.py's _build_section) and a
# Paragraph's own Sentence/Line-level children (produced when one paragraph
# is itself over budget). Deliberately excludes Chapter/Document: those
# children are Sections/Chapters, each its own semantic unit starting at a
# heading, and merging across that boundary is exactly what "never merge
# across a Chapter/Section heading boundary" forbids. Restricting the merge
# gate to these two levels enforces that for free - it never sees chunks
# from two different Sections or Chapters side by side to begin with.
_MERGE_GATE_LEVELS = frozenset({HierarchyLevel.SECTION, HierarchyLevel.PARAGRAPH})


@dataclass
class RenderedChunk:
    level: HierarchyLevel
    text: str
    element_ids: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    # Set when `_chunk_node` already knows the token count from its budget
    # check (the common leaf case) - `chunk()` reuses it instead of calling
    # count_tokens() on the same text a second time. None (the hard-split
    # path, which doesn't already have a per-piece count) falls back to
    # counting once in `chunk()`.
    token_count: int | None = None
    # See Node.is_table - carried through so the merge pass can refuse to
    # combine a table chunk with anything else.
    is_table: bool = False


class AdaptiveChunker(Chunker):
    def __init__(
        self,
        max_tokens_per_chunk: int = 4000,
        safety_margin: float = DEFAULT_SAFETY_MARGIN,
        min_tokens: int = DEFAULT_MIN_TOKENS,
    ):
        self._max_tokens = max_tokens_per_chunk
        self._budget = max(1, int(max_tokens_per_chunk * safety_margin))
        self._min_tokens = min_tokens

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
                    # NFC-normalized here (not just at DB-write time, see
                    # storage/postgres_store.py::save_chunks) so every
                    # in-memory consumer of Chunk.text - Knowledge Extraction
                    # prompts, record_chunk_embeddings - sees the same
                    # canonical form the DB row ends up with, not raw
                    # (possibly NFD, e.g. macOS/Drive-sourced) parser output.
                    # token_count is computed on the pre-normalization text
                    # (NFC composition only ever reduces codepoint count, and
                    # this was already just a chunking-time approximation).
                    text=to_nfc(item.text),
                    token_count=item.token_count if item.token_count is not None else count_tokens(item.text),
                    element_ids=item.element_ids,
                    page_range=page_range,
                )
            )
        return chunks

    def _chunk_node(self, node: Node) -> list[RenderedChunk]:
        if not node.text:
            return []
        node_tokens = count_tokens(node.text)
        if node_tokens <= self._budget:
            # Largest semantic unit that still fits the budget - keep it whole.
            return [
                RenderedChunk(
                    level=node.level,
                    text=node.text,
                    element_ids=node.element_ids,
                    pages=node.pages,
                    token_count=node_tokens,
                    is_table=node.is_table,
                )
            ]

        children: list[Node] | None
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
        if node.level in _MERGE_GATE_LEVELS:
            result = self._merge_small_siblings(result)
        return result

    def _merge_small_siblings(self, chunks: list[RenderedChunk]) -> list[RenderedChunk]:
        """Bottom-up pass: coalesce adjacent leaves so a lone bullet or a
        title-only slide paragraph doesn't survive as its own ~2-token
        chunk. `chunks` are always the flattened results of one node's own
        children (see the `_MERGE_GATE_LEVELS` call site above), so merging
        here can never cross a Chapter/Section boundary - those live at a
        level this pass doesn't run at.

        Greedy left-to-right: a running accumulator absorbs the next chunk
        when at least one side is under `min_tokens` (no point re-merging
        two already-adequately-sized neighbors) and the joined text still
        fits the budget. Table chunks (whole table paragraphs and
        split_table_by_rows() row-groups) never merge with anything - see
        Node.is_table.
        """
        if len(chunks) <= 1:
            return chunks
        merged = [chunks[0]]
        for chunk in chunks[1:]:
            prev = merged[-1]
            prev_tokens, chunk_tokens = prev.token_count, chunk.token_count
            can_merge = (
                not prev.is_table
                and not chunk.is_table
                and prev_tokens is not None
                and chunk_tokens is not None
                and (prev_tokens < self._min_tokens or chunk_tokens < self._min_tokens)
            )
            if can_merge:
                text = f"{prev.text}\n\n{chunk.text}"
                token_count = count_tokens(text)
                if token_count <= self._budget:
                    level = prev.level if level_rank(prev.level) <= level_rank(chunk.level) else chunk.level
                    merged[-1] = RenderedChunk(
                        level=level,
                        text=text,
                        element_ids=[*prev.element_ids, *chunk.element_ids],
                        pages=[*prev.pages, *chunk.pages],
                        token_count=token_count,
                    )
                    continue
            merged.append(chunk)
        return merged

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
        piece_token_slices = [tokens[i : i + self._budget] for i in range(0, len(tokens), self._budget)]
        return [
            RenderedChunk(
                level=HierarchyLevel.LINE,
                text=decode(piece_tokens),
                element_ids=node.element_ids,
                pages=node.pages,
                token_count=len(piece_tokens),
                is_table=node.is_table,
            )
            for piece_tokens in piece_token_slices
        ]
