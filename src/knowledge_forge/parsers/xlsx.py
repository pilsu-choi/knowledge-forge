import io

import openpyxl

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, DocumentElement, TableElement
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser


class XlsxParser(Parser):
    """Parses .xlsx into a Document using openpyxl - one TableElement per
    worksheet, sheet name as the table's caption.

    XLSX has no page concept - every element's `page` stays None.
    """

    supported_extensions = {".xlsx"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        try:
            elements: list[DocumentElement] = []
            workbook = openpyxl.load_workbook(io.BytesIO(item.content), data_only=True)
            for sheet in workbook.worksheets:
                rows = [
                    ["" if cell is None else str(cell) for cell in row]
                    for row in sheet.iter_rows(values_only=True)
                ]
                if not rows:
                    continue
                elements.append(
                    TableElement(
                        element_id=stable_id("element", item.uri, str(len(elements)), "table"),
                        rows=rows,
                        caption=sheet.title,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed parsing error
            raise ParsingError(f"Failed to parse xlsx '{item.uri}': {exc}") from exc

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )
