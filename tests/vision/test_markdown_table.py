from knowledge_forge.vision.markdown_table import parse_markdown_table


def test_parses_header_separator_and_data_rows():
    md = """
    | Company | Sales | Growth |
    |---|---:|---:|
    | A | 100M | 20% |
    | B | 80M | 15% |
    """
    rows = parse_markdown_table(md)
    assert rows == [
        ["Company", "Sales", "Growth"],
        ["A", "100M", "20%"],
        ["B", "80M", "15%"],
    ]


def test_ignores_blank_lines_and_non_table_text():
    md = "Here is a table:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nDone."
    rows = parse_markdown_table(md)
    assert rows == [["a", "b"], ["1", "2"]]


def test_empty_input_yields_empty_rows():
    assert parse_markdown_table("") == []
