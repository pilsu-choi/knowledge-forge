from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser


class XlsxParser(Parser):
    """Not yet implemented.

    Intended approach: `openpyxl` (already an optional dependency, the
    `xlsx` extra). `openpyxl.load_workbook(io.BytesIO(item.content),
    data_only=True)`, iterate `workbook.worksheets`, and emit one
    `TableElement` per sheet (`rows=[[str(cell.value or "") for cell in row]
    for row in sheet.iter_rows()]`), with the sheet name as `caption`.
    """

    supported_extensions = {".xlsx"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        raise NotImplementedError(
            "XlsxParser is a stub. Implement with openpyxl: "
            "load_workbook(io.BytesIO(item.content), data_only=True), "
            "one TableElement per worksheet."
        )
