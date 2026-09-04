import csv
import io

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.exceptions import ParsingError
from knowledge_forge.ids import stable_id
from knowledge_forge.models.document import Document, DocumentElement, TableElement
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser


class CsvParser(Parser):
    """Parses .csv into a single TableElement using the stdlib csv module.

    Unlike XLSX (multiple sheets, title/note rows around a table, merged
    multi-level headers - see parsers/xlsx.py), a CSV file carries no
    styling signal to detect any of that: every non-blank row becomes a row
    of one flat table, with the first row treated as the header
    (header_rows=1, matching every parser but xlsx). CSV has no page
    concept, so every element's `page` stays None.
    """

    supported_extensions = {".csv"}

    async def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        text = _decode(item.content)
        try:
            rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
        except csv.Error as exc:
            raise ParsingError(f"Failed to parse csv '{item.uri}': {exc}") from exc

        elements: list[DocumentElement] = []
        if rows:
            elements.append(
                TableElement(
                    element_id=stable_id("element", item.uri, "0", "table"),
                    rows=rows,
                    extraction_method="rule_based",
                )
            )

        return Document(
            document_id=stable_id("document", item.uri, item.content_hash),
            source=source_ref,
            elements=elements,
        )


def _decode(content: bytes) -> str:
    # utf-8-sig first: strips the BOM Excel prepends when it saves "CSV
    # UTF-8"; cp949 covers CSVs an Excel-for-Korean-Windows export instead
    # (the common non-UTF-8 case for this project's data) - see txt.py for
    # the same two-fallback shape with latin-1 as the always-succeeds last resort.
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")
