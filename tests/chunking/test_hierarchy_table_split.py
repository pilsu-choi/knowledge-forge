"""`split_table_by_rows` (chunking/hierarchy.py) used to re-render and
re-tokenize the whole accumulated group's text for every data row -
O(rows^2), which stalled a large xlsx sheet for 7+ minutes. It now counts
tokens incrementally (fixed header block once + each row's line once).

This file verifies two things the rewrite must preserve: (1) the SAME
groups as the old O(n^2) algorithm for normal input, and (2) a 5,000-row
table chunks in well under a few seconds.
"""

import time

from knowledge_forge.chunking.hierarchy import Node, split_table_by_rows
from knowledge_forge.chunking.tokenizer import count_tokens
from knowledge_forge.models.chunk import HierarchyLevel


def _old_split_table_by_rows(node: Node, budget: int) -> list[Node] | None:
    """The pre-fix implementation, kept here only as a reference oracle for
    the grouping-equivalence test below - re-renders and re-tokenizes the
    whole accumulated group on every row, deliberately O(rows^2)."""
    if not node.table_rows or len(node.table_rows) < 2:
        return None
    header_count = max(1, min(node.header_rows, len(node.table_rows) - 1))
    header_rows, data_rows = node.table_rows[:header_count], node.table_rows[header_count:]
    prefix_lines = [node.table_caption] if node.table_caption else []
    header_lines = [" | ".join(r) for r in header_rows]

    def render(rows: list[list[str]]) -> str:
        return "\n".join([*prefix_lines, *header_lines, *(" | ".join(r) for r in rows)])

    groups: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in data_rows:
        candidate = current + [row]
        if not current or count_tokens(render(candidate)) <= budget:
            current = candidate
        else:
            groups.append(current)
            current = [row]
    if current:
        groups.append(current)

    if len(groups) <= 1:
        return None

    return [
        Node(level=HierarchyLevel.PARAGRAPH, text=render(g), element_ids=node.element_ids, pages=node.pages)
        for g in groups
    ]


def _table_node(rows: list[list[str]], *, caption: str | None = None, header_rows: int = 1) -> Node:
    return Node(
        level=HierarchyLevel.PARAGRAPH,
        text="placeholder",  # not read by split_table_by_rows
        table_rows=rows,
        table_caption=caption,
        header_rows=header_rows,
    )


def _uniform_table(n_rows: int) -> list[list[str]]:
    header = ["id", "name", "amount"]
    return [header] + [[str(i), f"item-{i}", str(i * 10)] for i in range(n_rows)]


def _multi_header_table(n_rows: int) -> list[list[str]]:
    header = [["구분", "2025년", "2025년"], ["", "기본급", "가산율"]]
    return header + [[str(i), str(i * 100), f"{i}%"] for i in range(n_rows)]


def _ragged_width_table(n_rows: int) -> list[list[str]]:
    header = ["id", "note"]
    rows = [header]
    for i in range(n_rows):
        note = f"note-{i}" * (1 + i % 5)  # varying line length
        rows.append([str(i), note])
    return rows


def test_new_grouping_matches_old_grouping_on_a_uniform_table():
    for budget in (30, 100, 400):
        node = _table_node(_uniform_table(200), caption="Sheet1")
        old = _old_split_table_by_rows(node, budget)
        new = split_table_by_rows(node, budget)
        assert [n.text for n in (old or [])] == [n.text for n in (new or [])]


def test_new_grouping_matches_old_grouping_on_a_multi_header_table():
    for budget in (60, 150):
        node = _table_node(_multi_header_table(150), caption="Report", header_rows=2)
        old = _old_split_table_by_rows(node, budget)
        new = split_table_by_rows(node, budget)
        assert [n.text for n in (old or [])] == [n.text for n in (new or [])]


def test_new_grouping_matches_old_grouping_on_ragged_width_rows():
    for budget in (40, 120):
        node = _table_node(_ragged_width_table(120))
        old = _old_split_table_by_rows(node, budget)
        new = split_table_by_rows(node, budget)
        assert [n.text for n in (old or [])] == [n.text for n in (new or [])]


def test_new_grouping_matches_old_grouping_on_a_single_oversized_row():
    huge = "x" * 500
    node = _table_node([["id", "value"], ["1", huge]])
    old = _old_split_table_by_rows(node, 20)
    new = split_table_by_rows(node, 20)
    assert old is None and new is None


def test_5000_row_table_chunks_fast():
    node = _table_node(_uniform_table(5000), caption="Big Sheet")
    start = time.monotonic()
    groups = split_table_by_rows(node, 100)
    elapsed = time.monotonic() - start

    assert groups is not None
    assert len(groups) > 1
    assert elapsed < 3.0, f"split_table_by_rows took {elapsed:.2f}s for 5,000 rows - should be well under a few seconds"
