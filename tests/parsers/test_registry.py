import pytest

from knowledge_forge.exceptions import UnsupportedFormatError
from knowledge_forge.parsers.markdown import MarkdownParser
from knowledge_forge.parsers.registry import ParserRegistry
from knowledge_forge.parsers.txt import TxtParser


def _registry() -> ParserRegistry:
    registry = ParserRegistry()
    registry.register(MarkdownParser())
    registry.register(TxtParser())
    return registry


def test_get_parser_for_uses_uri_extension():
    parser = _registry().get_parser_for("/some/doc.md")
    assert isinstance(parser, MarkdownParser)


def test_get_parser_for_falls_back_to_filename_when_uri_has_no_extension():
    # Google Drive's webViewLink (the RawItem uri) carries no extension -
    # the real filename only lives in RawItem.metadata["name"].
    parser = _registry().get_parser_for(
        "https://drive.google.com/file/d/abc123/view?usp=drivesdk",
        filename="notes.txt",
    )
    assert isinstance(parser, TxtParser)


def test_get_parser_for_raises_when_neither_uri_nor_filename_has_a_known_extension():
    with pytest.raises(UnsupportedFormatError):
        _registry().get_parser_for(
            "https://drive.google.com/file/d/abc123/view?usp=drivesdk",
            filename="Makefile",
        )
