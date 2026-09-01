import re

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, DocumentElement, Heading, TextBlock
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.parsers.base import Parser

_BLANK_LINE = re.compile(r"\n\s*\n")


def _heading_text(item: RawItem, source_type: SourceType) -> str:
    if source_type == SourceType.GMAIL:
        subject = item.metadata.get("subject") or "(제목 없음)"
        return f"Gmail Thread: {subject}"
    channel = item.metadata.get("channel", "")
    kind = "스레드" if item.metadata.get("thread_ts") else "대화"
    return f"Slack #{channel} {kind}"


class ConversationParser(Parser):
    """Parses the merged transcripts produced by Context Reconstruction
    (see knowledge_forge.context) - one blank-line-separated block per
    original message, dispatched by URI scheme rather than file extension
    since Slack/Gmail RawItems have none."""

    supported_schemes = {"slack", "gmail"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        text = item.content.decode("utf-8", errors="replace")
        blocks = [b.strip() for b in _BLANK_LINE.split(text) if b.strip()]

        elements: list[DocumentElement] = [
            Heading(
                element_id=stable_id("element", item.uri, "heading"),
                level=1,
                text=_heading_text(item, source_ref.source_type),
            )
        ]
        elements.extend(
            TextBlock(element_id=stable_id("element", item.uri, str(i)), text=block)
            for i, block in enumerate(blocks)
        )

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )
