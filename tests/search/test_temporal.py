from datetime import datetime, timedelta, timezone

from knowledge_forge.search.temporal import parse_temporal_expression

# Friday 2026-09-04 15:30 UTC - a fixed reference so every test is
# deterministic regardless of when it actually runs.
NOW = datetime(2026, 9, 4, 15, 30, tzinfo=timezone.utc)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_no_temporal_expression_returns_none():
    assert parse_temporal_expression("인증 방식을 OAuth로 바꾼 결정", now=NOW) is None


def test_today():
    result = parse_temporal_expression("오늘 회의 내용 요약해줘", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 9, 4), _utc(2026, 9, 5))


def test_yesterday():
    result = parse_temporal_expression("어제 주요 논의사항 뭐야?", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 9, 3), _utc(2026, 9, 4))


def test_day_before_yesterday():
    result = parse_temporal_expression("그제 있었던 일", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 9, 2), _utc(2026, 9, 3))


def test_this_week():
    result = parse_temporal_expression("이번주 할 일", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 8, 31), _utc(2026, 9, 7))


def test_last_week():
    result = parse_temporal_expression("지난 주에 결정된 사항", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 8, 24), _utc(2026, 8, 31))


def test_this_month():
    result = parse_temporal_expression("이번 달 매출", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 9, 1), _utc(2026, 10, 1))


def test_last_month():
    result = parse_temporal_expression("저번달 진행 상황", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 8, 1), _utc(2026, 9, 1))


def test_this_quarter():
    result = parse_temporal_expression("이번 분기 목표", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 7, 1), _utc(2026, 10, 1))


def test_last_quarter():
    result = parse_temporal_expression("지난 분기 실적", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 4, 1), _utc(2026, 7, 1))


def test_recent_n_days():
    result = parse_temporal_expression("최근 3일 이슈", now=NOW)
    assert result.start == NOW - timedelta(days=3)
    assert result.end is None


def test_recent_n_weeks():
    result = parse_temporal_expression("최근 2주간 활동", now=NOW)
    assert result.start == _utc(2026, 8, 21, 15, 30)
    assert result.end is None


def test_recent_n_months():
    result = parse_temporal_expression("최근 2개월 트렌드", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 7, 1), None)


def test_explicit_year_month():
    result = parse_temporal_expression("2025년 3월에 있었던 일", now=NOW)
    assert (result.start, result.end) == (_utc(2025, 3, 1), _utc(2025, 4, 1))


def test_explicit_year_and_quarter_with_korean_suffix():
    result = parse_temporal_expression("2025년 3분기 매출", now=NOW)
    assert (result.start, result.end) == (_utc(2025, 7, 1), _utc(2025, 10, 1))


def test_explicit_year_and_quarter_q_notation():
    result = parse_temporal_expression("2025년 Q3 로드맵", now=NOW)
    assert (result.start, result.end) == (_utc(2025, 7, 1), _utc(2025, 10, 1))


def test_year_and_quarter_without_year_suffix():
    result = parse_temporal_expression("2025 Q3 로드맵", now=NOW)
    assert (result.start, result.end) == (_utc(2025, 7, 1), _utc(2025, 10, 1))


def test_bare_quarter_defaults_to_current_year():
    result = parse_temporal_expression("3분기 실적 정리", now=NOW)
    assert (result.start, result.end) == (_utc(2026, 7, 1), _utc(2026, 10, 1))


def test_year_only():
    result = parse_temporal_expression("2025년 전체 요약", now=NOW)
    assert (result.start, result.end) == (_utc(2025, 1, 1), _utc(2026, 1, 1))


def test_year_month_takes_priority_over_bare_year():
    # Must not be swallowed by the bare "YYYY년" pattern into a whole-year range.
    result = parse_temporal_expression("2025년 12월 계획", now=NOW)
    assert (result.start, result.end) == (_utc(2025, 12, 1), _utc(2026, 1, 1))


def test_label_is_human_readable():
    result = parse_temporal_expression("어제 무슨 일 있었어?", now=NOW)
    assert result.label == "어제"
