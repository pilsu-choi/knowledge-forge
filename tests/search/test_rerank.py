from knowledge_forge.search.rerank import DEFAULT_RRF_K, merge_and_rerank


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


def test_merge_and_rerank_empty_input_returns_empty_list():
    assert merge_and_rerank({}) == []


def test_default_rrf_k_is_a_positive_constant():
    assert DEFAULT_RRF_K > 0
