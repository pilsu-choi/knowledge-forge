from knowledge_forge.extraction.grouping import group_chunks
from knowledge_forge.models.chunk import Chunk, HierarchyLevel


def _chunk(i: int) -> Chunk:
    return Chunk(
        chunk_id=f"c{i}",
        document_id="d1",
        source_id="s1",
        hierarchy_level=HierarchyLevel.PARAGRAPH,
        sequence_index=i,
        text=f"chunk {i}",
        token_count=2,
    )


def test_group_size_one_yields_one_chunk_per_group():
    chunks = [_chunk(i) for i in range(3)]
    assert group_chunks(chunks, group_size=1) == [[chunks[0]], [chunks[1]], [chunks[2]]]


def test_group_size_two_bundles_consecutive_chunks_with_a_smaller_final_group():
    chunks = [_chunk(i) for i in range(3)]
    groups = group_chunks(chunks, group_size=2)
    assert groups == [[chunks[0], chunks[1]], [chunks[2]]]


def test_group_size_larger_than_chunk_count_yields_one_group():
    chunks = [_chunk(i) for i in range(2)]
    assert group_chunks(chunks, group_size=5) == [chunks]


def test_empty_chunk_list_yields_no_groups():
    assert group_chunks([], group_size=2) == []


def test_group_size_below_one_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        group_chunks([_chunk(0)], group_size=0)
