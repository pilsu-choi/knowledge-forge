from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from knowledge_forge.search.temporal import parse_temporal_expression

# Friday 2026-09-04 06:30 UTC (= 15:30 KST, same calendar day - deliberately
# NOT near the UTC/KST day boundary) so every test is deterministic and the
# arithmetic below stays simple. The UTC/KST-crossing case itself (a caller
# near UTC midnight) has its own dedicated test
# (`test_today_boundary_is_computed_in_seoul_time_not_utc`) using the exact
# probe from wiki/search-code-review-2026-09-14.md's 결함10.
NOW = datetime(2026, 9, 4, 6, 30, tzinfo=timezone.utc)

_SEOUL = ZoneInfo("Asia/Seoul")


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _kst(*args) -> datetime:
    """KST(서울) 지역시각을 UTC로 변환한다 - `search/temporal.py`가 모든 달력 경계를 서울시
    기준으로 계산하므로(그 모듈 docstring), 테스트 기댓값도 이 경로로 만든다."""
    return datetime(*args, tzinfo=_SEOUL).astimezone(timezone.utc)


def test_no_temporal_expression_returns_none():
    assert parse_temporal_expression("인증 방식을 OAuth로 바꾼 결정", now=NOW) is None


def test_today():
    result = parse_temporal_expression("오늘 회의 내용 요약해줘", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 9, 4), _kst(2026, 9, 5))


def test_yesterday():
    result = parse_temporal_expression("어제 주요 논의사항 뭐야?", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 9, 3), _kst(2026, 9, 4))


def test_day_before_yesterday():
    result = parse_temporal_expression("그제 있었던 일", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 9, 2), _kst(2026, 9, 3))


def test_this_week():
    result = parse_temporal_expression("이번주 할 일", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 8, 31), _kst(2026, 9, 7))


def test_last_week():
    result = parse_temporal_expression("지난 주에 결정된 사항", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 8, 24), _kst(2026, 8, 31))


def test_this_month():
    result = parse_temporal_expression("이번 달 매출", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 9, 1), _kst(2026, 10, 1))


def test_last_month():
    result = parse_temporal_expression("저번달 진행 상황", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 8, 1), _kst(2026, 9, 1))


def test_this_quarter():
    result = parse_temporal_expression("이번 분기 목표", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 7, 1), _kst(2026, 10, 1))


def test_last_quarter():
    result = parse_temporal_expression("지난 분기 실적", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 4, 1), _kst(2026, 7, 1))


def test_recent_n_days():
    result = parse_temporal_expression("최근 3일 이슈", now=NOW)
    assert result.start == NOW - timedelta(days=3)
    assert result.end is None


def test_recent_n_weeks():
    result = parse_temporal_expression("최근 2주간 활동", now=NOW)
    assert result.start == NOW - timedelta(weeks=2)
    assert result.end is None


def test_recent_n_months():
    result = parse_temporal_expression("최근 2개월 트렌드", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 7, 1), None)


def test_explicit_year_month():
    result = parse_temporal_expression("2025년 3월에 있었던 일", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 3, 1), _kst(2025, 4, 1))


def test_explicit_year_and_quarter_with_korean_suffix():
    result = parse_temporal_expression("2025년 3분기 매출", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 7, 1), _kst(2025, 10, 1))


def test_explicit_year_and_quarter_q_notation():
    result = parse_temporal_expression("2025년 Q3 로드맵", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 7, 1), _kst(2025, 10, 1))


def test_year_and_quarter_without_year_suffix():
    result = parse_temporal_expression("2025 Q3 로드맵", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 7, 1), _kst(2025, 10, 1))


def test_quarter_before_year_is_accepted():
    """"Q1 2025"(분기가 연도보다 앞선 어순) - 연도가 앞에 오는 어순만 받던 옛 정규식은 이걸
    현재 연도(2026년) Q1로 잘못 해석했다(`wiki/search-code-review-2026-09-14.md` 결함3)."""
    result = parse_temporal_expression("Q1 2025 보고서", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 1, 1), _kst(2025, 4, 1))


def test_bare_quarter_defaults_to_current_year():
    result = parse_temporal_expression("3분기 실적 정리", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 7, 1), _kst(2026, 10, 1))


def test_year_only():
    result = parse_temporal_expression("2025년 전체 요약", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 1, 1), _kst(2026, 1, 1))


def test_year_month_takes_priority_over_bare_year():
    # Must not be swallowed by the bare "YYYY년" pattern into a whole-year range.
    result = parse_temporal_expression("2025년 12월 계획", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 12, 1), _kst(2026, 1, 1))


def test_label_is_human_readable():
    result = parse_temporal_expression("어제 무슨 일 있었어?", now=NOW)
    assert result.label == "어제"


# --- 한글 수사 수량 ("최근 일주일") --------------------------------------------
# v0 골든셋 실측: `v0-relative_temporal-003`("최근 일주일 내 받은 Google Drive 공유 요청 메일")이
# 시간 조건을 전혀 못 뽑아 SQL에 since가 안 붙었다(steps에서 since=null 확인). 숫자만 받던
# `_RECENT_RE`가 원인이라, 한국어에서 더 자연스러운 수사 표기를 아래에 못 박는다.


def test_recent_one_week_written_as_korean_word():
    """골든셋 v0-relative_temporal-003의 실제 표현. 숫자 없이 '일주일'만 나온다."""
    result = parse_temporal_expression("최근 일주일 내 받은 Google Drive 공유 요청 메일", now=NOW)
    assert result is not None
    assert result.start == NOW - timedelta(weeks=1)
    assert result.end is None


def test_recent_native_numeral_month():
    """'한 달'/'한달' - 띄어쓰기 유무 둘 다. 월 단위는 기존 '최근 N개월'과 같은 월초 절단."""
    for text in ("최근 한 달 이슈", "최근 한달 이슈"):
        result = parse_temporal_expression(text, now=NOW)
        assert result is not None, text
        assert (result.start, result.end) == (_kst(2026, 8, 1), None), text


def test_recent_fused_day_words():
    """수사와 단위가 한 낱말로 굳은 형태 - 분해가 안 되므로 별도 표에서 (수, 단위)로 편다."""
    assert parse_temporal_expression("최근 하루 동안", now=NOW).start == NOW - timedelta(days=1)
    assert parse_temporal_expression("최근 이틀 이슈", now=NOW).start == NOW - timedelta(days=2)
    assert parse_temporal_expression("최근 사흘 이슈", now=NOW).start == NOW - timedelta(days=3)


def test_recent_few_days_is_a_recall_first_window():
    """'며칠'은 정확한 수가 없다. 규칙 단계는 recall 우선이라 7일로 넉넉히 잡는다."""
    result = parse_temporal_expression("최근 며칠 사이 장애", now=NOW)
    assert result.start == NOW - timedelta(days=7)


def test_recent_year_written_as_korean_word():
    result = parse_temporal_expression("최근 일 년간 매출 추이", now=NOW)
    assert (result.start, result.end) == (_kst(2025, 9, 1), None)
    assert parse_temporal_expression("최근 한 해 동안", now=NOW).start == _kst(2025, 9, 1)


def test_recent_sino_numeral_with_month_unit():
    result = parse_temporal_expression("최근 삼개월 실적", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 6, 1), None)


def test_recent_digit_with_month_word_unit():
    """'개월'뿐 아니라 '달'도 월 단위로 받는다 - '최근 3달'."""
    result = parse_temporal_expression("최근 3달 트렌드", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 6, 1), None)


def test_last_prefix_also_forms_a_relative_span():
    """'지난 일주일'은 달력 주(지난주)가 아니라 now 기준 롤링 7일이다."""
    result = parse_temporal_expression("지난 일주일 배포 내역", now=NOW)
    assert result.start == NOW - timedelta(weeks=1)
    assert result.end is None


def test_last_week_still_means_the_calendar_week():
    """회귀 방지: '지난 주'는 수량 표현이 없으므로 기존 달력 주 경계를 그대로 유지해야 한다."""
    result = parse_temporal_expression("지난 주에 결정된 사항", now=NOW)
    assert (result.start, result.end) == (_kst(2026, 8, 24), _kst(2026, 8, 31))


def test_recent_without_quantity_is_not_a_time_range():
    """회귀 방지: '최근'만 있는 건 정렬(latest) 신호지 범위가 아니다. 한자어 수사가 명사의
    첫 음절과 겹쳐 오탐 나는 경우('최근 이슈'의 '이', '최근 오해'의 '오')도 여기서 막는다."""
    for text in ("최근 이슈 정리", "최근 오해가 있었던 건", "최근 사고 보고서", "최근 자료"):
        assert parse_temporal_expression(text, now=NOW) is None, text


def test_a_day_level_date_is_one_day_not_the_whole_month():
    """연-월-일이 `_YEAR_MONTH_RE`보다 먼저 잡혀야 한다. 뭉개지면 한 달치를 다 끌어와
    "정답을 전부 담고도 10위 밖"이 된다(실측: `v1-temporal-002`가
    exhaustive_recall 1.000 / ndcg@10 0.000이었다)."""
    result = parse_temporal_expression("2026년 8월 13일에 받은 메일", now=NOW)
    assert result is not None
    assert result.start == _kst(2026, 8, 13)
    assert result.end is not None
    assert (result.end - result.start).days == 1


def test_iso_dates_are_parsed_and_bare_numbers_are_not():
    result = parse_temporal_expression("2026-08-13에 받은 메일", now=NOW)
    assert result is not None
    assert result.start == _kst(2026, 8, 13)
    # 앞뒤로 숫자가 더 붙으면 날짜가 아니다(버전 문자열/전화번호).
    assert parse_temporal_expression("빌드 12026-08-139 로그", now=NOW) is None


def test_the_dependent_noun_il_is_not_a_day_number():
    """"2025년 3월에 있었던 일"의 "일"은 날짜가 아니다 - 일 자리에 숫자를 반드시 요구한다."""
    result = parse_temporal_expression("2025년 3월에 있었던 일", now=NOW)
    assert result is not None
    assert result.start == _kst(2025, 3, 1)


def test_an_impossible_day_falls_through_to_the_month():
    """2월 30일 같은 없는 날짜는 틀린 하루로 좁히는 대신 한 달로 넓게 잡는다."""
    result = parse_temporal_expression("2026년 2월 30일 회의록", now=NOW)
    assert result is not None
    assert result.start == _kst(2026, 2, 1)


# --- "지난 4월"/"지난 오월"/"지난 12일" (wiki/search-code-review-2026-09-14.md 결함4/10) ---


def test_last_bare_month_resolves_to_the_most_recent_past_occurrence():
    """"지난 4월"은 "이미 지나간 가장 최근 4월"이다 - 지금이 9월이면 올해 4월이 이미 지났으므로
    올해, 아직 안 지났으면 작년이다. 이전엔 `_RECENT_RE`의 월 단위가 bare "월"도 받아들여
    "최근 4개월"로 오독됐다(실측: `start=2026-05-01, label='최근 4개월'`)."""
    result = parse_temporal_expression("지난 4월 회의록", now=NOW)
    assert result is not None
    assert (result.start, result.end) == (_kst(2026, 4, 1), _kst(2026, 5, 1))
    assert result.label == "2026년 4월"


def test_last_bare_month_falls_back_to_last_year_before_it_happens_this_year():
    early_now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    result = parse_temporal_expression("지난 4월 회의록", now=early_now)
    assert result is not None
    assert result.start == _kst(2025, 4, 1)


def test_last_korean_numeral_month_is_not_a_recent_span():
    """"지난 오월"은 "최근 5개월"이 아니라 5월(May)을 가리킨다(한자어 수사 "오"+"월")."""
    result = parse_temporal_expression("지난 오월 워크숍 자료", now=NOW)
    assert result is not None
    assert (result.start, result.end) == (_kst(2026, 5, 1), _kst(2026, 6, 1))


def test_last_n_days_is_left_unparsed_not_a_recent_duration():
    """"지난 12일"은 "최근 12일"(기간)이 아니다 - 한국어에서는 거의 항상 (어느 달의) 12일이라는
    날짜를 가리킨다. 어느 달인지 확정할 근거가 없어 규칙 단계는 좁히지 않고 그대로 둔다(틀리게
    좁히는 것보다 안전 - 결정은 `wiki/search-code-review-2026-09-14.md`의 "수정 내역" 참고)."""
    assert parse_temporal_expression("지난 12일 지출 내역", now=NOW) is None
    # "최근 12일"은 여전히 기간이다 - "최근"만 일 단위 수량의 기간 해석을 허용한다.
    result = parse_temporal_expression("최근 12일 지출 내역", now=NOW)
    assert result is not None
    assert result.start == NOW - timedelta(days=12)


def test_today_boundary_is_computed_in_seoul_time_not_utc():
    """리뷰 결함10(`wiki/search-code-review-2026-09-14.md`): 자정 경계가 UTC 기준이면 KST
    기준으로는 아직 전날이다. now=2026-09-14T03:00Z는 KST로 이미 09-14 정오라 "오늘"은
    09-14여야 한다."""
    now = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
    result = parse_temporal_expression("오늘 회의록", now=now)
    assert result is not None
    assert result.start == datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)
    assert result.end == datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
