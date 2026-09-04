"""Pure-function tests for storage/document_title.py and the lexical-search
helpers in storage/postgres_store.py - no database needed, so these always
run (the DB-backed counterparts live in test_lexical_store.py)."""

import unicodedata
from datetime import datetime, timezone

from knowledge_forge.models.document import Heading, TextBlock
from knowledge_forge.storage import document_title
from knowledge_forge.storage.document_title import derive_document_title, first_heading_text, slack_excerpt
from knowledge_forge.storage.postgres_store import _lexical_snippet, _like_pattern, _token_patterns

_AT = datetime(2026, 8, 11, 9, 18, tzinfo=timezone.utc)


def _derive(**overrides):
    kwargs = dict(metadata_title=None, source_type="google_drive", source_metadata=None, content_at=_AT, first_heading=None)
    kwargs.update(overrides)
    return derive_document_title(**kwargs)


def test_parser_metadata_title_wins_over_everything():
    assert _derive(metadata_title="  Authored   Title ", source_metadata={"name": "file.pdf"}, first_heading="H") == "Authored Title"


def test_google_drive_name_is_used_when_parser_has_no_title():
    assert _derive(source_metadata={"name": "2025_SH_업무_보고서.pdf", "file_id": "abc"}, first_heading="H") == "2025_SH_업무_보고서.pdf"


def test_gmail_subject_is_used():
    assert _derive(source_type="gmail", source_metadata={"subject": "[NH농협] 고객사 산출물 기간 변경"}) == "[NH농협] 고객사 산출물 기간 변경"


def test_slack_thread_becomes_channel_plus_date():
    title = _derive(source_type="slack", source_metadata={"channel": "C0BGQEQG1L6", "thread_ts": "1786438991.7"}, first_heading="Slack 스레드")

    assert title == "#C0BGQEQG1L6 2026-08-11"


def test_slack_prefers_channel_name_and_tolerates_missing_date():
    title = _derive(source_type="slack", source_metadata={"channel": "C1", "channel_name": "#general"}, content_at=None)

    assert title == "#general"


def test_first_heading_is_the_last_resort():
    assert _derive(source_metadata={"file_id": "x"}, first_heading="  분기 보고 ") == "분기 보고"


def test_returns_none_when_nothing_is_usable():
    assert _derive(source_metadata={"name": "   "}, first_heading="") is None


def test_first_heading_text_reads_models_and_jsonb_dicts_alike():
    models = [TextBlock(element_id="e1", text="intro"), Heading(element_id="e2", level=1, text="제목"), Heading(element_id="e3", level=2, text="둘째")]
    dicts = [e.model_dump(mode="json") for e in models]

    assert first_heading_text(models) == "제목"
    assert first_heading_text(dicts) == "제목"
    assert first_heading_text([]) is None


def test_like_pattern_escapes_underscore_percent_and_backslash():
    assert _like_pattern("2025_SH 100% a\\b") == "%2025\\_SH 100\\% a\\\\b%"


def test_token_patterns_split_on_whitespace_and_cap_the_count():
    assert _token_patterns("SH 보고서") == ["%SH%", "%보고서%"]
    assert len(_token_patterns(" ".join(str(i) for i in range(20)))) == 8


def test_lexical_snippet_centers_on_the_match_and_normalizes_whitespace():
    text = "x " * 400 + "ERR_CONNECTION_RESET   happened\n\nhere " + "y " * 400

    snippet = _lexical_snippet(text, "err_connection_reset", 120)

    assert "ERR_CONNECTION_RESET happened here" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= 122


def test_lexical_snippet_falls_back_to_the_head_when_nothing_matches():
    assert _lexical_snippet("a  b   c", "zzz", 3) == "a b"


# --- 유니코드 정규화 (NFC) -------------------------------------------------
# macOS/Google Drive를 거친 파일명은 한글 자모가 분해된 NFD로 들어온다. 그대로 제목이
# 되면 NFC로 친 질의가 `title ILIKE '%q%'`에 구조적으로 못 맞는다(text_normalize.py).

_NFC_NAME = "프로젝트 투입 관리_2026-031_한국자산관리공사.xlsx"
_NFD_NAME = unicodedata.normalize("NFD", _NFC_NAME)


def test_drive_name_in_nfd_is_stored_as_nfc():
    assert _NFD_NAME != _NFC_NAME  # 전제: 두 표현은 실제로 다른 문자열이다
    assert _derive(source_metadata={"name": _NFD_NAME}) == _NFC_NAME


def test_gmail_subject_mixing_nfd_and_nfc_is_fully_composed():
    """실제 버그 모양 - 파일명 부분만 NFD이고 나머지 한글은 NFC인 혼합 정규화 제목."""
    subject = f"이제 ‘{_NFD_NAME}’의 소유자가 되었습니다"

    title = _derive(source_type="gmail", source_metadata={"subject": subject})

    assert title == unicodedata.normalize("NFC", subject)
    assert _NFC_NAME in (title or "")  # NFC 질의가 파이썬 `in`으로도 맞는다


def test_nfd_heading_is_normalized_too():
    assert _derive(first_heading=_NFD_NAME) == _NFC_NAME


def test_title_is_truncated_after_normalization():
    """자르기가 정규화보다 먼저면 NFD 3코드포인트가 1글자로 합쳐지면서 자르는 위치가
    달라진다. 300자 상한은 NFC 기준 글자 수여야 한다."""
    long_nfd = unicodedata.normalize("NFD", "한" * 400)

    title = _derive(source_metadata={"name": long_nfd})

    assert title == "한" * 300


# --- 슬랙 발췌 (storage/document_title.py::slack_excerpt) --------------------------


def test_slack_excerpt_strips_speaker_prefix_emoji_and_link_markup():
    # 화자 접두어 `U…: `는 백필 경로(documents.elements의 첫 텍스트 블록)에만 붙어 있다.
    raw = "U0BDNDBHWLV: :pushpin: [투입관리 원장] 실제 투입 MM 입력  요청 <https://x/y|원장 링크>"

    assert slack_excerpt(raw, 200) == "[투입관리 원장] 실제 투입 MM 입력 요청 원장 링크"


def test_slack_excerpt_keeps_bare_url_when_the_link_has_no_label():
    assert slack_excerpt("보세요 <https://git.example/a.git>", 200) == "보세요 https://git.example/a.git"


def test_slack_excerpt_returns_none_for_nothing_usable():
    assert slack_excerpt(None, 200) is None
    assert slack_excerpt("   ", 200) is None
    assert slack_excerpt(123, 200) is None
    # limit 0 = 발췌 끄기. derive_document_title이 이걸로 제목에 발췌를 붙일지 정한다.
    assert slack_excerpt("아무 메시지", 0) is None


def test_slack_title_appends_the_excerpt_when_it_is_enabled(monkeypatch):
    # 길이를 바꿔도 조립이 맞는지 지킨다(기본값 120과 무관하게).
    monkeypatch.setattr(document_title, "_SLACK_EXCERPT_CHARS", 40)
    title = _derive(
        source_type="slack",
        source_metadata={"channel": "C1", "channel_name": "general", "first_message": "U0BDNDBHWLV: 3차 인터뷰 체크리스트 파일 전달드립니다"},
    )

    assert title == "#general 2026-08-11 3차 인터뷰 체크리스트 파일 전달드립니다"


def test_slack_title_includes_the_excerpt_by_default():
    # 기본값이 120으로 켜졌다(커밋 09296fb가 `title_types` 술어를 지워 켜는 조건이 충족됐다).
    # 이 테스트가 기본값이 켜져 있다는 계약을 지킨다 - 끄면 제목 경로의 슬랙 recall이
    # 42/66에서 0/66으로 돌아간다(document_title.py의 상수 주석 참고).
    title = _derive(
        source_type="slack",
        source_metadata={"channel": "C1", "channel_name": "general", "first_message": "U0BDNDBHWLV: 어떤 메시지든"},
    )

    assert title == "#general 2026-08-11 어떤 메시지든"


def test_slack_title_is_channel_and_date_only_when_the_excerpt_is_off(monkeypatch):
    monkeypatch.setattr(document_title, "_SLACK_EXCERPT_CHARS", 0)
    title = _derive(
        source_type="slack",
        source_metadata={"channel": "C1", "channel_name": "general", "first_message": "U0BDNDBHWLV: 어떤 메시지든"},
    )

    assert title == "#general 2026-08-11"


def test_slack_title_survives_a_missing_channel_when_the_excerpt_is_on(monkeypatch):
    monkeypatch.setattr(document_title, "_SLACK_EXCERPT_CHARS", 40)

    assert _derive(source_type="slack", source_metadata={"first_message": "회의록 공유"}) == "2026-08-11 회의록 공유"
