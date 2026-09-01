from pathlib import PurePosixPath
from urllib.parse import urlparse

from knowledge_forge.exceptions import UnsupportedFormatError
from knowledge_forge.parsers.base import Parser
from knowledge_forge.parsers.conversation import ConversationParser
from knowledge_forge.parsers.docx import DocxParser
from knowledge_forge.parsers.html import HtmlParser
from knowledge_forge.parsers.hwpx import HwpxParser
from knowledge_forge.parsers.image import ImageParser
from knowledge_forge.parsers.markdown import MarkdownParser
from knowledge_forge.parsers.pdf import PDFParser
from knowledge_forge.parsers.pptx import PptxParser
from knowledge_forge.parsers.txt import TxtParser
from knowledge_forge.parsers.xlsx import XlsxParser


class ParserRegistry:
    """Dispatches a uri/path to the Parser registered for its extension, or -
    for sources with no file extension, like the Slack/Gmail conversations
    Context Reconstruction produces (see knowledge_forge.context) - its URI
    scheme.

    Stub parsers (xlsx/pptx/image/hwpx) are registered too, so an
    unimplemented-but-recognized format raises NotImplementedError from the
    parser itself rather than UnsupportedFormatError from the registry -
    those are different failure modes for a caller to handle.
    """

    def __init__(self) -> None:
        self._parsers: dict[str, Parser] = {}
        self._scheme_parsers: dict[str, Parser] = {}

    def register(self, parser: Parser) -> None:
        for ext in parser.supported_extensions:
            self._parsers[ext.lower()] = parser
        for scheme in parser.supported_schemes:
            self._scheme_parsers[scheme] = parser

    def get_parser_for(self, uri: str) -> Parser:
        parsed = urlparse(uri)
        scheme_parser = self._scheme_parsers.get(parsed.scheme)
        if scheme_parser is not None:
            return scheme_parser

        # urlparse handles both plain paths and uri schemes like file:// consistently.
        path = parsed.path or uri
        extension = PurePosixPath(path).suffix.lower()
        parser = self._parsers.get(extension)
        if parser is None:
            raise UnsupportedFormatError(
                f"No parser registered for extension '{extension}' (uri: {uri})"
            )
        return parser


def default_registry() -> ParserRegistry:
    registry = ParserRegistry()
    for parser in (
        PDFParser(),
        DocxParser(),
        HtmlParser(),
        MarkdownParser(),
        TxtParser(),
        XlsxParser(),
        PptxParser(),
        ImageParser(),
        HwpxParser(),
        ConversationParser(),
    ):
        registry.register(parser)
    return registry
