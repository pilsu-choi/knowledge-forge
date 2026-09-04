"""`storage/title_match.py`의 토큰 집합 판정 - DB 없이 도는 순수 함수 테스트.

여기서 지키는 계약은 하나로 요약된다: **1.0은 anchor를 뜻하므로 아껴 쓴다.**
`search/retrieve.py::_claim_anchor`가 제목 1.0 하나를 "이 질의의 답인 문서"로 확정하고
다른 전략의 히트까지 그 문서로 좁히기 때문에, 가짜 1.0 하나가 그 질의의 검색 전체를
망친다(모듈 docstring 참고)."""

from knowledge_forge.storage.title_match import (
    MATCH_EXACT_SET,
    MATCH_NONE,
    MATCH_PARTIAL,
    MATCH_SUBSET_AMBIGUOUS,
    MATCH_SUBSET_UNIQUE,
    match_titles,
    query_tokens,
    title_tokens,
)


def test_tokens_ignore_separators_and_case_and_split_camel_case():
    assert title_tokens("README_ProjectIntel_Dev_Handover.md") == {
        "readme",
        "projectintel",  # 쪼갠 조각과 원래 덩어리를 둘 다 남긴다
        "project",
        "intel",
        "dev",
        "handover",
        "md",
    }
    assert title_tokens("codex-README.md") == {"codex", "readme", "md"}
    assert title_tokens("2025_SH_업무_보고서.pdf") == {"2025", "sh", "업무", "보고서", "pdf"}
    # 한글/영문이 붙어 있어도 문자 종류 경계에서 끊는다.
    assert title_tokens("인수인계pptx") == {"인수인계", "pptx", "인수인계pptx"}


def test_query_tokens_turn_off_the_tier_for_sentence_length_queries():
    """이름이 아니라 문장이면 토큰 집합 판정 자체를 포기한다 - 앞부분만 잘라 쓰면
    "질의 토큰이 전부 제목에 있다"가 다른 뜻이 돼 버린다."""
    assert query_tokens("a b c d e f g h i j") == frozenset()
    assert query_tokens("Dev Handover README") == {"dev", "handover", "readme"}


def test_order_and_separator_do_not_matter_when_the_title_is_unique():
    """실측된 회귀: 질의 "Dev Handover README"는 토큰이 전부 들어 있는데도 순서와 구분자가
    달라 substring tier(1.0)를 못 맞췄다(v0-navigational-004, ndcg@10 0.500)."""
    titles = ["README_ProjectIntel_Dev_Handover.md", "codex-README.md", "projectintel-development-handover.pptx"]

    matches = match_titles("Dev Handover README", titles, [False, False, False])

    assert [m.kind for m in matches] == [MATCH_SUBSET_UNIQUE, MATCH_NONE, MATCH_NONE]
    assert matches[0].score == 1.0


def test_exact_token_set_scores_1_0_even_next_to_other_candidates():
    matches = match_titles(
        "handover guide projectintel md",
        ["projectintel-handover-guide.md", "projectintel-handover-file-bundle.md"],
        [False, False],
    )

    assert matches[0].kind == MATCH_EXACT_SET and matches[0].score == 1.0


def test_a_subset_match_is_capped_below_1_0_when_more_than_one_title_qualifies():
    """부분집합 문서가 여럿이면 그중 무엇도 "이 질의와 같은 제목의 문서 하나"가 아니다.
    0.8 상한이 중요하다 - `_ANCHOR_MIN_GAP`(0.2) 덕에 1위가 1.0인 질의에서 이 tier가
    2위를 올려도 anchor 격차가 깨지지 않는다."""
    titles = ["README_ProjectIntel_Dev_Handover.md", "projectintel-development-handover.pptx"]

    matches = match_titles("projectintel handover", titles, [False, False])

    assert [m.kind for m in matches] == [MATCH_SUBSET_AMBIGUOUS, MATCH_SUBSET_AMBIGUOUS]
    assert all(m.score == 0.8 for m in matches)


def test_a_substring_1_0_elsewhere_blocks_the_unique_promotion():
    """이미 substring으로 1.0인 문서가 따로 있으면 부분집합 문서는 1.0을 못 받는다 -
    안 그러면 한 질의에 1.0이 둘이 되어 있던 anchor가 ambiguous로 무너진다
    (`v0-ambiguous-002`의 실패 방식과 같다)."""
    matches = match_titles("readme", ["codex-README.md", "README_ProjectIntel_Dev_Handover.md"], [True, True])

    assert all(m.kind == MATCH_SUBSET_AMBIGUOUS and m.score == 0.8 for m in matches)


def test_partial_overlap_is_a_separate_tier_well_below_1_0():
    matches = match_titles("projectintel dev guide", ["projectintel-handover-guide.md"], [False])

    assert matches[0].kind == MATCH_PARTIAL
    assert 0.5 < matches[0].score <= 0.8


def test_one_shared_token_is_not_a_match():
    """흔한 토큰 하나(`md`, `보고서`)가 겹쳤다고 후보로 올리면 제목 검색이 아니라
    확장자 검색이 된다."""
    assert match_titles("배포 계획 md", ["projectintel-handover-guide.md"], [False])[0].kind == MATCH_NONE


def test_korean_query_against_an_english_title_is_out_of_reach():
    """알려진 한계 - `v0-navigational-003`("인수인계 pptx" vs
    `projectintel-development-handover.pptx`)는 공통 토큰이 확장자뿐이라 토큰 집합으로는
    풀리지 않는다. 번역/별칭 사전이 필요한 별개 문제이고, 이 tier가 그걸 억지로 맞히려
    들면(문턱을 낮추면) 가짜 1.0이 쏟아진다."""
    assert match_titles("인수인계 pptx", ["projectintel-development-handover.pptx"], [False])[0].kind == MATCH_NONE
