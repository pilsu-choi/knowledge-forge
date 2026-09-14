import pytest

from knowledge_forge.extraction.grouping import group_chunks
from knowledge_forge.models.chunk import Chunk, HierarchyLevel


def _chunk(i: int, token_count: int = 2) -> Chunk:
    return Chunk(
        chunk_id=f"c{i}",
        document_id="d1",
        source_id="s1",
        hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=i,
        text=f"chunk {i}",
        token_count=token_count,
    )


def test_small_chunks_are_bundled_up_to_the_token_budget():
    # 2 tokens each -> 3 fit in a 6-token budget, the 4th starts a new group.
    chunks = [_chunk(i) for i in range(4)]
    groups = group_chunks(chunks, max_tokens=6)
    assert groups == [[chunks[0], chunks[1], chunks[2]], [chunks[3]]]


def test_a_single_chunk_over_budget_stays_alone_rather_than_erroring():
    chunks = [_chunk(0, token_count=100), _chunk(1, token_count=2), _chunk(2, token_count=2)]
    groups = group_chunks(chunks, max_tokens=10)
    assert groups == [[chunks[0]], [chunks[1], chunks[2]]]


def test_max_tokens_larger_than_total_yields_one_group():
    chunks = [_chunk(i) for i in range(3)]
    assert group_chunks(chunks, max_tokens=4000) == [chunks]


def test_default_max_tokens_bundles_several_small_chunks_into_one_call():
    chunks = [_chunk(i, token_count=50) for i in range(10)]  # 500 tokens total, default budget 4000
    assert group_chunks(chunks) == [chunks]


def test_empty_chunk_list_yields_no_groups():
    assert group_chunks([], max_tokens=100) == []


def test_max_tokens_below_one_is_rejected():
    with pytest.raises(ValueError):
        group_chunks([_chunk(0)], max_tokens=0)
