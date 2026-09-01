import re

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, TextBlock
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser

_BLANK_LINE = re.compile(r"\n\s*\n")


class TxtParser(Parser):
    """Plain text has no structural markup, so this only splits into paragraphs
    (blank-line separated) - no heading detection is possible here."""

    supported_extensions = {".txt"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            text = item.content.decode("utf-8")
        except UnicodeDecodeError:
            text = item.content.decode("latin-1")

        paragraphs = [p.strip() for p in _BLANK_LINE.split(text) if p.strip()]
        elements = [
            TextBlock(element_id=stable_id("element", item.uri, str(i)), text=p)
            for i, p in enumerate(paragraphs)
        ]

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )
