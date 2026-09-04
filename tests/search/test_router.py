import pytest

from knowledge_forge.search.router import FILE_SEARCH, VECTOR_SEARCH, route_query


def test_route_query_raises_without_any_available_source():
    with pytest.raises(ValueError):
        route_query("아무거나", [])


def test_route_query_returns_the_only_available_source_regardless_of_query():
    assert route_query("무엇이든", [FILE_SEARCH]).sources == [FILE_SEARCH]
    assert route_query("무엇이든", [VECTOR_SEARCH]).sources == [VECTOR_SEARCH]


def test_route_query_prefers_file_search_for_quoted_exact_phrases():
    decision = route_query('"인증 방식을 OAuth로"', [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]


def test_route_query_prefers_file_search_for_id_like_tokens():
    decision = route_query("ko-abc123 관련 문서 찾아줘", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]


def test_route_query_runs_both_sources_for_short_keyword_queries():
    decision = route_query("김철수", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]


def test_route_query_prefers_vector_search_for_natural_language_questions():
    decision = route_query("김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [VECTOR_SEARCH, FILE_SEARCH]


def test_route_query_prefers_vector_search_for_question_mark_queries():
    decision = route_query("이 문서 어디에 있어?", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [VECTOR_SEARCH, FILE_SEARCH]


def test_route_query_runs_both_sources_when_ambiguous():
    decision = route_query("프로젝트 진행 상황 정리", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]


def test_route_query_falls_back_to_file_search_when_vector_search_unavailable():
    decision = route_query("김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", [FILE_SEARCH])

    assert decision.sources == [FILE_SEARCH]
