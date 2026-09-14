"""Builds the Document -> Chapter -> Section -> Paragraph structural tree,
and produces Sentence -> Line nodes on demand when a Paragraph is still too
large (see chunking.adaptive).

Document/Chapter/Section/Paragraph are built eagerly from the Document's
elements, since that structure is free (already implied by heading levels).
Sentence/Line are deliberately NOT pre-built for every paragraph - most
paragraphs never need to be split that far, and building sentence/line
nodes we'll never use would be wasted work for every document.
"""

import re
from dataclasses import dataclass, field

from knowledge_forge.chunking.tokenizer import count_tokens
from knowledge_forge.models.chunk import HierarchyLevel
from knowledge_forge.models.document import (
    Document,
    DocumentElement,
    Heading,
    ImageElement,
    ListElement,
    TableElement,
    TextBlock,
)

_LEVEL_ORDER = [
    HierarchyLevel.DOCUMENT,
    HierarchyLevel.CHAPTER,
    HierarchyLevel.SECTION,
    HierarchyLevel.PARAGRAPH,
    HierarchyLevel.SENTENCE,
    HierarchyLevel.LINE,
]

# Falls back to this when a formal sentence boundary can't be found (e.g. no
# `kss` installed, or non-Korean text) - lossy on Korean prose without
# Western punctuation, documented as a known quality limitation.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?。！？])\s+")

# `kss` (pecab backend, no mecab/C++ analyzer installed - see the `korean`
# extra) is a Viterbi-style morpheme analyzer whose cost grows worse than
# linearly in input length: ~0.3s at 300 chars, ~11s at 2,000 chars on
# ordinary Korean prose (see wiki/adaptive-chunking.md for the full table).
# A single long paragraph (e.g. a PDF page or Slack message with no
# newlines) can therefore stall ingestion for minutes. _kss_windows() bounds
# how much text a single kss call ever sees by cutting only at cheap,
# already-mostly-safe boundaries (newlines, or _SENTENCE_BOUNDARY_RE) so a
# paragraph at or under the cap - the overwhelming majority - is still fed
# to kss whole, unchanged from before this cap existed.
_KSS_WINDOW_CHARS = 1000
_KSS_WINDOW_BOUNDARY_RE = re.compile(rf"\n|{_SENTENCE_BOUNDARY_RE.pattern}")


def _kss_windows(text: str) -> list[str]:
    """Partition text into <= _KSS_WINDOW_CHARS-char windows, cutting only at
    _KSS_WINDOW_BOUNDARY_RE matches so windows are a lossless, boundary-
    aligned partition of text (concatenating them reproduces it exactly). A
    run longer than the cap with no boundary in it at all (e.g. one huge
    whitespace-free token) gets a hard character cut instead - kss can't
    find a sentence in it either way, and adaptive.py's _hard_split() will
    re-split it by tokens further down the recursion regardless.
    """
    if len(text) <= _KSS_WINDOW_CHARS:
        return [text]
    pieces = []
    pos = 0
    for m in _KSS_WINDOW_BOUNDARY_RE.finditer(text):
        pieces.append(text[pos : m.end()])
        pos = m.end()
    pieces.append(text[pos:])

    windows = []
    current = ""
    for piece in pieces:
        while len(piece) > _KSS_WINDOW_CHARS:
            if current:
                windows.append(current)
                current = ""
            windows.append(piece[:_KSS_WINDOW_CHARS])
            piece = piece[_KSS_WINDOW_CHARS:]
        if current and len(current) + len(piece) > _KSS_WINDOW_CHARS:
            windows.append(current)
            current = piece
        else:
            current += piece
    if current:
        windows.append(current)
    return windows


def next_level(level: HierarchyLevel) -> HierarchyLevel | None:
    idx = _LEVEL_ORDER.index(level)
    if idx + 1 >= len(_LEVEL_ORDER):
        return None
    return _LEVEL_ORDER[idx + 1]


@dataclass
class Node:
    level: HierarchyLevel
    text: str
    element_ids: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    # None for Paragraph (and finer) nodes - their children, if any, are
    # produced lazily by split_to_next_level() rather than stored here.
    children: list["Node"] | None = None
    # Set only on a Paragraph node that renders a TableElement - lets
    # split_table_by_rows() split by row (with the header repeated) instead
    # of the generic sentence/line split, which is meaningless for tabular
    # data. None for every other node.
    table_rows: list[list[str]] | None = None
    table_caption: str | None = None
    # How many leading rows of table_rows are header rows (see
    # TableElement.header_rows). Meaningless when table_rows is None.
    header_rows: int = 1

    @property
    def page_range(self) -> tuple[int, int] | None:
        if not self.pages:
            return None
        return (min(self.pages), max(self.pages))


def _render_element_body(element: DocumentElement) -> str:
    if isinstance(element, Heading):
        return f"{'#' * element.level} {element.text}"
    if isinstance(element, TextBlock):
        return element.text
    if isinstance(element, ListElement):
        lines = [
            f"{i + 1}. {item}" if element.ordered else f"- {item}"
            for i, item in enumerate(element.items)
        ]
        return "\n".join(lines)
    if isinstance(element, TableElement):
        lines = [" | ".join(row) for row in element.rows]
        if element.caption:
            lines.insert(0, element.caption)
        return "\n".join(lines)
    if isinstance(element, ImageElement):
        return element.ocr_text or element.alt_text or ""
    raise TypeError(f"Unknown document element type: {type(element)!r}")


def render_element_text(element: DocumentElement) -> str:
    body = _render_element_body(element)
    # Tables/images render tabular or free-form transcribed text where a
    # per-line indent prefix would just be noise (or corrupt " | " column
    # alignment) - only headings/paragraphs/lists carry the source
    # document's own indentation.
    if not body or not element.indent_level or isinstance(element, (TableElement, ImageElement)):
        return body
    prefix = "  " * element.indent_level
    return "\n".join(prefix + line for line in body.splitlines())


def _split_by_heading_level(
    elements: list[DocumentElement], level: int
) -> list[list[DocumentElement]]:
    groups: list[list[DocumentElement]] = []
    current: list[DocumentElement] = []
    for element in elements:
        if isinstance(element, Heading) and element.level == level:
            if current:
                groups.append(current)
            current = [element]
        else:
            current.append(element)
    if current:
        groups.append(current)
    return groups


def _join(nodes: list[Node]) -> tuple[str, list[str], list[int]]:
    text = "\n\n".join(n.text for n in nodes if n.text)
    element_ids = [eid for n in nodes for eid in n.element_ids]
    pages = [p for n in nodes for p in n.pages]
    return text, element_ids, pages


def _build_paragraph(element: DocumentElement) -> Node:
    return Node(
        level=HierarchyLevel.PARAGRAPH,
        text=render_element_text(element),
        element_ids=[element.element_id],
        pages=[element.page] if element.page is not None else [],
        children=None,
        table_rows=element.rows if isinstance(element, TableElement) else None,
        table_caption=element.caption if isinstance(element, TableElement) else None,
        header_rows=element.header_rows if isinstance(element, TableElement) else 1,
    )


def _build_section(elements: list[DocumentElement]) -> Node:
    paragraphs = [_build_paragraph(e) for e in elements]
    text, element_ids, pages = _join(paragraphs)
    return Node(
        level=HierarchyLevel.SECTION,
        text=text,
        element_ids=element_ids,
        pages=pages,
        children=paragraphs,
    )


def _build_chapter(elements: list[DocumentElement]) -> Node:
    section_groups = _split_by_heading_level(elements, level=2)
    if not section_groups:
        section_groups = [elements]
    sections = [_build_section(g) for g in section_groups]
    text, element_ids, pages = _join(sections)
    return Node(
        level=HierarchyLevel.CHAPTER,
        text=text,
        element_ids=element_ids,
        pages=pages,
        children=sections,
    )


def build_tree(document: Document) -> Node:
    chapter_groups = _split_by_heading_level(document.elements, level=1)
    if not chapter_groups:
        # No level-1 headings at all - the whole document is one implicit chapter.
        chapter_groups = [document.elements] if document.elements else [[]]
    chapters = [_build_chapter(g) for g in chapter_groups]
    text, element_ids, pages = _join(chapters)
    return Node(
        level=HierarchyLevel.DOCUMENT,
        text=text,
        element_ids=element_ids,
        pages=pages,
        children=chapters,
    )


def split_sentences(text: str) -> list[str]:
    try:
        import kss  # type: ignore

        sentences = [s for window in _kss_windows(text) for s in kss.split_sentences(window)]
    except Exception:
        sentences = _SENTENCE_BOUNDARY_RE.split(text)
    return [s.strip() for s in sentences if s and s.strip()]


def split_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def split_to_next_level(node: Node) -> list[Node] | None:
    """Produce this node's children one level finer, or None if it can't be
    split any further (already at LINE, or the split yields no more than
    one piece - e.g. a single sentence/line with no internal boundary).
    """
    level = next_level(node.level)
    if level is None:
        return None
    if level == HierarchyLevel.SENTENCE:
        pieces = split_sentences(node.text)
    elif level == HierarchyLevel.LINE:
        pieces = split_lines(node.text)
    else:
        raise AssertionError(f"split_to_next_level called on unexpected level {node.level}")
    if len(pieces) <= 1:
        return None
    return [
        Node(level=level, text=piece, element_ids=node.element_ids, pages=node.pages, children=None)
        for piece in pieces
    ]


def split_table_by_rows(node: Node, budget: int) -> list[Node] | None:
    """Row-aware split for a Paragraph node that renders a table: groups
    data rows greedily under `budget`, repeating the header row (and
    caption) in every group so each emitted chunk stays self-describing
    independent of the others - a plain sentence/line split would instead
    cut mid-row and drop the header, losing which column is which.

    Returns None (falling through to the generic split/hard-split path) when
    there's no header+data-row structure to split on, or when grouping makes
    no forward progress (e.g. a single oversized row) - the caller's normal
    recursion then re-checks and, if still too big, falls back further.
    """
    if not node.table_rows or len(node.table_rows) < 2:
        return None
    # Clamp so at least one data row always remains, even if header_rows was
    # mis-detected as covering the whole table.
    header_count = max(1, min(node.header_rows, len(node.table_rows) - 1))
    header_rows, data_rows = node.table_rows[:header_count], node.table_rows[header_count:]
    prefix_lines = [node.table_caption] if node.table_caption else []
    header_lines = [" | ".join(r) for r in header_rows]

    def render(rows: list[list[str]]) -> str:
        return "\n".join([*prefix_lines, *header_lines, *(" | ".join(r) for r in rows)])

    groups: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in data_rows:
        candidate = current + [row]
        if not current or count_tokens(render(candidate)) <= budget:
            current = candidate
        else:
            groups.append(current)
            current = [row]
    if current:
        groups.append(current)

    if len(groups) <= 1:
        return None

    return [
        Node(level=HierarchyLevel.PARAGRAPH, text=render(g), element_ids=node.element_ids, pages=node.pages)
        for g in groups
    ]
