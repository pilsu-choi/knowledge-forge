import re

from lxml import html as lxml_html
from lxml.html import HtmlElement

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import (
    Document,
    DocumentElement,
    Heading,
    ImageElement,
    ListElement,
    TableElement,
    TextBlock,
)
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser

_WHITESPACE_RE = re.compile(r"\s+")
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Dropped entirely (no text, no recursion) - non-content or non-visible tags.
_SKIP_TAGS = {"script", "style", "noscript", "head", "title", "meta", "link"}


def _clean_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


class HtmlParser(Parser):
    """Parses HTML into a Document by walking the DOM in document order.

    Headings/paragraphs(+blockquotes)/lists/tables/images map to their
    Document element counterparts; every other tag (div, section, span,
    body, html, ...) is a transparent container the walk recurses through
    without emitting anything itself. lxml.html is a recovering parser (the
    same tolerance browsers apply), so malformed/unclosed markup parses
    rather than failing.
    """

    supported_extensions = {".html", ".htm"}

    async def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            root = lxml_html.fromstring(item.content)
            elements: list[DocumentElement] = []
            self._walk(root, item.uri, elements)
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse html '{item.uri}': {exc}") from exc

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )

    def _walk(self, element: HtmlElement, uri: str, elements: list[DocumentElement]) -> None:
        for child in element:
            if not isinstance(child.tag, str):  # comments / processing instructions
                continue
            tag = child.tag.lower()

            if tag in _SKIP_TAGS:
                continue

            if tag in _HEADING_TAGS:
                text = _clean_text(child.text_content())
                if text:
                    elements.append(
                        Heading(
                            element_id=stable_id("element", uri, str(len(elements)), "heading"),
                            level=int(tag[1]),
                            text=text,
                        )
                    )
                continue

            if tag in ("p", "blockquote"):
                text = _clean_text(child.text_content())
                if text:
                    elements.append(
                        TextBlock(
                            element_id=stable_id("element", uri, str(len(elements)), "text"),
                            text=text,
                        )
                    )
                continue

            if tag in ("ul", "ol"):
                items = [
                    _clean_text(li.text_content())
                    for li in child.findall("li")
                    if _clean_text(li.text_content())
                ]
                if items:
                    elements.append(
                        ListElement(
                            element_id=stable_id("element", uri, str(len(elements)), "list"),
                            items=items,
                            ordered=tag == "ol",
                        )
                    )
                continue

            if tag == "table":
                rows = [
                    [_clean_text(cell.text_content()) for cell in tr if cell.tag in ("td", "th")]
                    for tr in child.iter("tr")
                ]
                rows = [row for row in rows if row]
                if rows:
                    elements.append(
                        TableElement(
                            element_id=stable_id("element", uri, str(len(elements)), "table"),
                            rows=rows,
                            extraction_method="rule_based",
                        )
                    )
                continue

            if tag == "img":
                elements.append(
                    ImageElement(
                        element_id=stable_id("element", uri, str(len(elements)), "image"),
                        uri=child.get("src"),
                        alt_text=child.get("alt") or None,
                    )
                )
                continue

            # Transparent container: div, section, article, span, body, html, ...
            self._walk(child, uri, elements)
