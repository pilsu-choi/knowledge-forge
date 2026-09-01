from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.document import Document
from knowledge_forge.models.source import SourceRef
from knowledge_forge.parsers.base import Parser


class HwpxParser(Parser):
    """Not yet implemented.

    No viable pure-Python library exists for HWPX on Linux: `pyhwpx` wraps
    Hangul-program COM automation (Windows-only, requires the Hangul word
    processor installed - unusable server-side), while `hwp5`/`pyhwp`
    target the legacy binary `.hwp` format, not the zip/XML-based `.hwpx`.

    Intended approach: HWPX is a zip container of XML parts (OWPML schema,
    structurally similar in spirit to OOXML/DOCX). A custom parser using
    stdlib `zipfile` to read `Contents/section*.xml` and `lxml` to walk the
    OWPML element tree (`<hp:p>` paragraphs, `<hp:tbl>` tables, `<hp:pic>`
    images) would be required. This is a real implementation effort, scoped
    as separate follow-up work - see wiki/roadmap.md.
    """

    supported_extensions = {".hwpx"}

    def parse(self, item: RawItem, source_ref: SourceRef) -> Document:
        raise NotImplementedError(
            "HwpxParser is a stub - no pure-Python HWPX library exists for "
            "Linux. Needs a custom zipfile+lxml parser against the HWPX "
            "OWPML XML schema. See this module's docstring and wiki/parsers.md."
        )
