"""EmbeddingRecord's grain invariant (exactly one of knowledge_object_id /
chunk_id, matching target_kind) and the deterministic surrogate PK that
replaced the old (knowledge_object_id, embedding_model) composite key."""

import pytest

from knowledge_forge.embedding.base import EmbeddingRecord


def _record(**overrides):
    base = dict(
        target_kind="knowledge_object",
        knowledge_object_id="ko1",
        document_id="d1",
        embedding_model="m",
        embedding=[0.0],
        embedded_text="t",
        embedded_text_hash="h",
        project_id="p",
    )
    base.update(overrides)
    return EmbeddingRecord(**base)


def test_knowledge_object_record_exposes_ko_as_target():
    rec = _record()
    assert rec.target_id == "ko1"
    assert rec.chunk_id is None


def test_chunk_record_exposes_chunk_as_target():
    rec = _record(target_kind="chunk", knowledge_object_id=None, chunk_id="c1")
    assert rec.target_id == "c1"
    assert rec.knowledge_object_id is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"knowledge_object_id": None},  # KO kind without a KO id
        {"chunk_id": "c1"},  # KO kind with both ids
        {"target_kind": "chunk", "knowledge_object_id": None},  # chunk kind without a chunk id
        {"target_kind": "chunk", "chunk_id": "c1"},  # chunk kind with both ids
        {"target_kind": "document"},  # unknown grain
    ],
)
def test_record_rejects_a_grain_id_mismatch(overrides):
    with pytest.raises(ValueError):
        _record(**overrides)


def test_embedding_id_is_deterministic_per_target_and_model_and_distinct_across_grains():
    ko_a = _record()
    ko_b = _record(embedding=[1.0], embedded_text="other")  # same target+model, different payload
    ko_other_model = _record(embedding_model="m2")
    chunk = _record(target_kind="chunk", knowledge_object_id=None, chunk_id="ko1")  # same raw id string, chunk grain

    assert ko_a.embedding_id == ko_b.embedding_id  # re-embedding upserts in place
    assert ko_a.embedding_id != ko_other_model.embedding_id  # a new model is a new row
    assert ko_a.embedding_id != chunk.embedding_id  # grain is part of the key
