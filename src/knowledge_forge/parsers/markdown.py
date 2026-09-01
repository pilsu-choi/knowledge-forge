from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import (
    Document,
    DocumentElement,
    Heading,
    ListElement,
    TableElement,
    TextBlock,
)
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser


def _inline_text(node: SyntaxTreeNode) -> str:
    """Concatenate the plain text of an inline node's children (bold/code/etc collapse to text)."""
    inline = node.children[0] if node.children else None
    if inline is None or inline.type != "inline":
        return ""
    return "".join(c.content for c in inline.children) or inline.content


class MarkdownParser(Parser):
    """Parses Markdown (CommonMark + GFM tables/strikethrough) into a Document.

    GFM table support comes from markdown-it-py's built-in "gfm-like" preset,
    not a separate plugin - mdit_py_plugins has no dedicated table plugin.
    Linkify is disabled since it needs the optional linkify-it-py package
    and we don't need autolinking for structural parsing.
    """

    supported_extensions = {".md", ".markdown"}

    def __init__(self) -> None:
        self._md = MarkdownIt("gfm-like").disable("linkify")

    async def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            text = item.content.decode("utf-8")
            root = SyntaxTreeNode(self._md.parse(text))
            elements: list[DocumentElement] = []
            for i, node in enumerate(root.children):
                element = self._convert(node, item.uri, i)
                if element is not None:
                    elements.append(element)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse markdown '{item.uri}': {exc}") from exc

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )

    def _convert(self, node: SyntaxTreeNode, uri: str, index: int) -> DocumentElement | None:
        element_id = stable_id("element", uri, str(index), node.type)

        if node.type == "heading":
            level = int(node.tag[1]) if node.tag and node.tag[1:].isdigit() else 1
            return Heading(element_id=element_id, level=level, text=_inline_text(node))

        if node.type == "paragraph":
            text = _inline_text(node)
            return TextBlock(element_id=element_id, text=text) if text else None

        if node.type in ("bullet_list", "ordered_list"):
            items = [_inline_text(li.children[0]) for li in node.children if li.children]
            return ListElement(
                element_id=element_id, items=items, ordered=node.type == "ordered_list"
            )

        if node.type == "table":
            rows: list[list[str]] = []
            for section in node.children:  # thead / tbody
                for row in section.children:
                    rows.append([_inline_text(cell) for cell in row.children])
            return TableElement(element_id=element_id, rows=rows)

        return None
