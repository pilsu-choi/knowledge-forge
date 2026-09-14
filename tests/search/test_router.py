import pytest

from knowledge_forge.search.router import FILE_SEARCH, VECTOR_SEARCH, has_exact_match_signal, route_query


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


def test_route_query_runs_both_sources_for_natural_language_questions():
    # Order carries no signal for any caller (`search/planner.py::_execute_subquery`
    # only checks membership) - route_query() always returns both when both are
    # available, so this only checks the reason names the right heuristic.
    decision = route_query("김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]
    assert "자연어 질문" in decision.reason


def test_route_query_runs_both_sources_for_question_mark_queries():
    decision = route_query("이 문서 어디에 있어?", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]
    assert "자연어 질문" in decision.reason


def test_route_query_runs_both_sources_when_ambiguous():
    decision = route_query("프로젝트 진행 상황 정리", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]


def test_route_query_falls_back_to_file_search_when_vector_search_unavailable():
    decision = route_query("김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘", [FILE_SEARCH])

    assert decision.sources == [FILE_SEARCH]


@pytest.mark.parametrize(
    "query",
    [
        "2025_SH_업무_보고서 찾아줘",  # digit-leading, Hangul-mixed joined name
        "왜 CASE3-EDGE-rl 결과가 다른지 설명해줘",  # hyphen-joined, inside a natural-language question
        "보고서.pdf 어디 있어?",  # extension alone
        "(2025_SH_업무_보고서.xlsx)",  # wrapped in brackets
    ],
)
def test_has_exact_match_signal_recognizes_document_names(query):
    assert has_exact_match_signal(query)


@pytest.mark.parametrize(
    "query",
    [
        "업무-보고 정리",  # two joined pieces is ordinary prose (Latin pairs like "well-known" are _CODE_ID_RE's call)
        "프로젝트 진행 상황 정리",
        "김철수가 참여한 프로젝트가 왜 실패했는지 이유를 설명해줘",
        "pdf 파일로 저장하는 방법",  # the word, not an extension
    ],
)
def test_has_exact_match_signal_ignores_ordinary_prose(query):
    assert not has_exact_match_signal(query)


def test_route_query_treats_a_document_name_in_a_question_as_an_exact_signal():
    decision = route_query("왜 2025_SH_업무_보고서 매출 수치가 다른지 이유를 설명해줘", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]
    assert "파일명" in decision.reason


def test_has_exact_match_signal_recognizes_code_id_before_a_korean_particle():
    # `\b` never closes between two Hangul-adjacent code points (Python's `\b`
    # treats Hangul as \w), so a plain `\b...\b` id regex fails right where a
    # Korean particle immediately follows the id - `_CODE_ID_RE` (imported from
    # `search/constraints.py`) uses ASCII-only lookaround instead and doesn't.
    assert has_exact_match_signal("ERR_404에서 뭐가 문제야")


def test_route_query_routes_a_code_id_before_a_korean_particle_as_exact_signal():
    decision = route_query("ERR_404에서 뭐가 문제야", [FILE_SEARCH, VECTOR_SEARCH])

    assert decision.sources == [FILE_SEARCH, VECTOR_SEARCH]
    assert "정확 일치" in decision.reason
