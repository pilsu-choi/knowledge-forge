from knowledge_forge.chunking.hierarchy import render_element_text
from knowledge_forge.models.document import Heading, ListElement, TableElement, TextBlock


def test_render_element_text_prefixes_indented_text_block():
    element = TextBlock(element_id="t1", text="nested line", indent_level=2)
    assert render_element_text(element) == "    nested line"


def test_render_element_text_indents_every_line_of_a_list():
    element = ListElement(element_id="l1", items=["a", "b"], indent_level=1)
    assert render_element_text(element) == "  - a\n  - b"


def test_render_element_text_indents_a_heading():
    element = Heading(element_id="h1", level=2, text="Sub", indent_level=1)
    assert render_element_text(element) == "  ## Sub"


def test_render_element_text_is_unchanged_without_an_indent_level():
    element = TextBlock(element_id="t1", text="flat line")
    assert render_element_text(element) == "flat line"


def test_render_element_text_ignores_indent_level_on_tables():
    element = TableElement(element_id="tb1", rows=[["a", "b"]], indent_level=3)
    assert render_element_text(element) == "a | b"
