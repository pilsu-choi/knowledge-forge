"""Lenient GitHub-Flavored-Markdown table parser.

Used to turn a vision model's markdown table transcription back into the
`rows: list[list[str]]` shape TableElement already expects, so the rest of
the pipeline (adaptive chunker's row-aware table splitting included) never
has to know a table came from a VLM instead of a deterministic parser.
"""

import re

_SEPARATOR_ROW_RE = re.compile(r"^[\s|:-]+$")


def parse_markdown_table(markdown: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        if _SEPARATOR_ROW_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    return rows
