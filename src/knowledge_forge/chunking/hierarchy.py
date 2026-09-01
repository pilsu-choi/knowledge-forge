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

    @property
    def page_range(self) -> tuple[int, int] | None:
        if not self.pages:
            return None
        return (min(self.pages), max(self.pages))


def render_element_text(element: DocumentElement) -> str:
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

        sentences = kss.split_sentences(text)
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
