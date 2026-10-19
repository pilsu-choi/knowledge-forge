from knowledge_forge.search.rerank import DEFAULT_RRF_K, merge_and_rerank, merge_by_arm


def test_merge_and_rerank_preserves_order_for_a_single_source():
    hits = [{"path": "a"}, {"path": "b"}, {"path": "c"}]

    result = merge_and_rerank({"file_search": hits})

    assert [hit["path"] for hit in result] == ["a", "b", "c"]
    assert all(hit["retrieval_sources"] == ["file_search"] for hit in result)


def test_merge_and_rerank_merges_hits_sharing_a_knowledge_object_id():
    file_hits = [{"path": "p1", "title": "Auth 결정", "knowledge_object_id": "ko1"}]
    vector_hits = [{"knowledge_object_id": "ko1", "score": 0.9}]

    result = merge_and_rerank({"file_search": file_hits, "vector_search": vector_hits})

    assert len(result) == 1
    hit = result[0]
    assert sorted(hit["retrieval_sources"]) == ["file_search", "vector_search"]
    # Fields from both sources survive the merge.
    assert hit["path"] == "p1"
    assert hit["title"] == "Auth 결정"
    assert hit["score"] == 0.9


def test_merge_and_rerank_does_not_merge_hits_with_different_identities():
    file_hits = [{"path": "p1"}]
    vector_hits = [{"knowledge_object_id": "ko1", "score": 0.9}]

    result = merge_and_rerank({"file_search": file_hits, "vector_search": vector_hits})

    assert len(result) == 2
    assert {tuple(hit["retrieval_sources"]) for hit in result} == {("file_search",), ("vector_search",)}


def test_merge_and_rerank_ranks_a_hit_found_by_both_sources_above_a_single_source_hit():
    file_hits = [
        {"path": "shared", "knowledge_object_id": "ko-shared"},
        {"path": "file-only"},
    ]
    vector_hits = [
        {"knowledge_object_id": "ko-vector-only", "score": 0.99},
        {"knowledge_object_id": "ko-shared", "score": 0.5},
    ]

    result = merge_and_rerank({"file_search": file_hits, "vector_search": vector_hits})

    # "ko-shared" is only rank 1 in file_search and rank 2 in vector_search,
    # yet it should outrank vector_search's rank-1-only hit because it
    # accumulates RRF score from two lists instead of one.
    assert result[0]["knowledge_object_id"] == "ko-shared"
    assert sorted(result[0]["retrieval_sources"]) == ["file_search", "vector_search"]


def test_merge_and_rerank_first_source_field_wins_on_overlap():
    file_hits = [{"path": "p1", "knowledge_object_id": "ko1", "type": "from-file"}]
    vector_hits = [{"knowledge_object_id": "ko1", "type": "from-vector"}]

    result = merge_and_rerank({"file_search": file_hits, "vector_search": vector_hits})

    assert result[0]["type"] == "from-file"


def test_merge_and_rerank_drops_the_per_hit_retrieval_source_tag():
    hits = [{"path": "p1", "retrieval_source": "file_search"}]

    result = merge_and_rerank({"file_search": hits})

    assert "retrieval_source" not in result[0]
    assert result[0]["retrieval_sources"] == ["file_search"]


def test_merge_and_rerank_counts_a_hit_once_per_list():
    # c1 twice in one list must not outvote c2, which two lists agree on
    result = merge_and_rerank({"a": [{"chunk_id": "c1"}, {"chunk_id": "c2"}, {"chunk_id": "c1"}], "b": [{"chunk_id": "c2"}]})

    assert [hit["chunk_id"] for hit in result] == ["c2", "c1"]
    assert result[1]["rerank_score"] == 1.0 / (DEFAULT_RRF_K + 1)


def test_merge_and_rerank_empty_input_returns_empty_list():
    assert merge_and_rerank({}) == []


def test_default_rrf_k_is_a_positive_constant():
    assert DEFAULT_RRF_K > 0


def test_merge_and_rerank_dedups_chunk_grain_hits_by_chunk_id_without_merging_into_ko_hits():
    """Chunk-grain vector hits (KF_EMBEDDING_SOURCE=chunk) carry `chunk_id`
    and no `knowledge_object_id` - two hits for the same chunk collapse,
    but a chunk hit never merges with a KO hit even from the same document
    (they are different retrieval units; the evaluation compares them at
    document level itself)."""
    vector_hits = [
        {"chunk_id": "c1", "document_id": "d1", "score": 0.9},
        {"knowledge_object_id": "ko1", "document_id": "d1", "score": 0.8},
    ]
    plan_hits = [{"chunk_id": "c1", "document_id": "d1", "score": 0.7}]

    result = merge_and_rerank({"vector_search": vector_hits, "plan_search": plan_hits})

    assert len(result) == 2
    chunk_hit = next(hit for hit in result if hit.get("chunk_id") == "c1")
    assert sorted(chunk_hit["retrieval_sources"]) == ["plan_search", "vector_search"]
    ko_hit = next(hit for hit in result if hit.get("knowledge_object_id") == "ko1")
    assert ko_hit["retrieval_sources"] == ["vector_search"]


def _doc(document_id, chunk_id=None):
    return {"document_id": document_id, "chunk_id": chunk_id or f"c-{document_id}"}


def test_merge_by_arm_gives_each_arm_one_vote_regardless_of_list_count():
    # 어휘 arm이 term 리스트 3개를 내도 벡터와 같은 한 표다: 어휘 1위 L, 벡터 1위 V.
    # 소스 리스트마다 투표하면 L이 3표를 받아 V를 이긴다.
    sources = {
        "lexical_search#a": [_doc("L"), _doc("X")],
        "lexical_search#b": [_doc("L")],
        "lexical_search#c": [_doc("L")],
        "vector_search": [_doc("V"), _doc("X")],
    }
    per_list = [h["document_id"] for h in merge_and_rerank(sources)]
    per_arm = merge_by_arm(sources)
    assert per_list[0] == "L"
    assert per_arm[0]["document_id"] == "X"  # 두 arm이 모두 찾은 문서가 위
    assert {h["document_id"]: h["rerank_score"] for h in per_arm}["L"] == {h["document_id"]: h["rerank_score"] for h in per_arm}["V"]
    assert per_arm[0]["retrieval_sources"] == ["lexical_search", "vector_search"]


def test_merge_by_arm_folds_chunk_hits_of_one_document_into_one_hit():
    result = merge_by_arm({"vector_search": [_doc("D", "c1"), _doc("D", "c2"), _doc("E")]})
    assert [h["document_id"] for h in result] == ["D", "E"]
    assert result[0]["rerank_score"] == 1.0 / (DEFAULT_RRF_K + 1)
