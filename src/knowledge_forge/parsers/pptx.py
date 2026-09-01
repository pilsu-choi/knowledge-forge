from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser


class PptxParser(Parser):
    """Not yet implemented.

    Intended approach: `python-pptx` (already an optional dependency, the
    `pptx` extra). `Presentation(io.BytesIO(item.content))`, iterate
    `prs.slides` (page = slide index): the slide's title placeholder ->
    `Heading(level=1, ...)`; other `shape.text_frame` paragraphs ->
    `TextBlock`; `shape.has_table` -> `TableElement`.
    """

    supported_extensions = {".pptx"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        raise NotImplementedError(
            "PptxParser is a stub. Implement with python-pptx: "
            "Presentation(io.BytesIO(item.content)), slide title -> Heading, "
            "text frames -> TextBlock, tables -> TableElement, page=slide index."
        )
