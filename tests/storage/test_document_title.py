"""Pure-function tests for storage/document_title.py and the lexical-search
helpers in storage/postgres_store.py - no database needed, so these always
run (the DB-backed counterparts live in test_lexical_store.py)."""

from datetime import datetime, timezone

from knowledge_forge.models.document import Heading, TextBlock
from knowledge_forge.storage.document_title import derive_document_title, first_heading_text
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
