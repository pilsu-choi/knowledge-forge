"""Temporal Search's query-understanding half (wiki/search-architecture.md's
"⑥ Temporal Search") - turns a relative/quarter-style Korean/English time
expression embedded in a query ("어제", "지난주", "2025년 Q3", "최근 3개월")
into an explicit `[start, end)` UTC datetime range `file_search`/
`vector_search` can filter on.

Plain regex + calendar arithmetic, not an LLM call - same reasoning as
`search/router.py`: resolving "어제" to an explicit range is a mechanical,
deterministic computation once `now` is fixed, so spending an LLM round trip
on it would only add latency/cost without improving accuracy. This also
mirrors `search/router.py`'s scope boundary - a real Query Understanding
layer (entity extraction, conversation-aware "그 프로젝트는?" resolution,
temporal *join* across KnowledgeObject types) stays out of scope, see
wiki/search-architecture.md's "① Query Understanding"/"⑥ Temporal Search".

Every boundary this module returns is a UTC instant (the range is compared
against `Source.modified_at`/`collected_at`, both UTC) - but every calendar
*boundary calculation* ("자정", "이번 달 1일", "이번 분기 시작") happens in
Asia/Seoul (KST, UTC+9, no DST) via `local_date_start`, not in UTC, then
converts to UTC. "오늘"/"이번 달" are the caller's KST calendar day/month, not
a UTC one - a fixed +9h offset (2026-09-14T03:00Z is already 2026-09-14 in
KST but would still be 2026-09-13 for a caller further west) used to make
"오늘" resolve to the wrong calendar day for a Korean caller up to a third of
the time (any query sent between 00:00 and 09:00 KST). `now` itself is still
accepted/returned as UTC (`datetime.now(timezone.utc)` by default) - only the
*boundary arithmetic* moved to KST.

The range this module produces is compared against the *source's* real-world
timestamp (`Source.modified_at`, falling back to `collected_at`) by
`file_search`/`vector_search`, not `KnowledgeObject.extracted_at` (when the
pipeline happened to process it) - a batch extraction run stamps every KO it
touches with the same `extracted_at`, which made a real "지난달" query
against it return nothing regardless of what was actually being asked. See
wiki/search-architecture.md's ⑥ section for how this was found (a blind
Text-to-SQL test) and fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SEOUL_TZ = ZoneInfo("Asia/Seoul")


@dataclass
class TimeRange:
    """Half-open interval: `start` inclusive, `end` exclusive. Either bound
    may be None (open-ended) - `parse_temporal_expression` only ever leaves
    `end` open (for "최근 N일/주/개월", which means "from N ago until now",
    not a closed calendar bucket)."""

    start: datetime | None
    end: datetime | None
    label: str


_TODAY_RE = re.compile(r"오늘")
_DAY_BEFORE_YESTERDAY_RE = re.compile(r"그저께|그제")
_YESTERDAY_RE = re.compile(r"어제")
_THIS_WEEK_RE = re.compile(r"이번\s?주")
_LAST_WEEK_RE = re.compile(r"(?:지난|저번)\s?주")
_THIS_MONTH_RE = re.compile(r"이번\s?달")
_LAST_MONTH_RE = re.compile(r"(?:지난|저번)\s?달")
_THIS_QUARTER_RE = re.compile(r"이번\s?분기")
_LAST_QUARTER_RE = re.compile(r"(?:지난|저번)\s?분기")

# --- "최근 N일" 계열의 수량 표기 -------------------------------------------------
# 이전 패턴은 `최근\s*(\d+)\s*(일|주|개?월)`로 아라비아 숫자만 받았다. v0 골든셋 실측에서
# `v0-relative_temporal-003`("최근 일주일 내 받은 Google Drive 공유 요청 메일")이 시간 조건을
# 통째로 놓쳐(`steps`의 since=null) SQL에 기간 필터가 안 붙었다 - 한국어는 이 자리를 숫자 없이
# 쓰는 쪽이 오히려 더 자연스럽다("일주일", "한 달", "이틀").
#
# 수량을 세 갈래로 나눈다:
# (a) 융합형 - 수사와 단위가 한 낱말로 굳어 분해가 안 되는 것("하루", "이틀", "며칠").
# (b) 숫자 + 단위 - 기존 계약 그대로. 단위에 "달"/"년"을 추가했다.
# (c) 수사 + 단위 - 고유어(한/두/세…)와 한자어(일/이/삼…) 수사.
#     (c)에서 단위 "일"과 "해"는 일부러 뺐다. "최근 오일"/"최근 사일"은 실제로 쓰지 않는
#     표현인데 이를 허용하면 "최근 오일 가격"이 5일 범위로, "최근 오해"가 5년 범위로 오독된다.
#     숫자를 쓴 "최근 5일"은 (b)가, "최근 한 해"는 (a)가 받으므로 손실이 없다.
_FUSED_RECENT: tuple[tuple[str, int, str], ...] = (
    (r"하루", 1, "일"),
    (r"이틀", 2, "일"),
    (r"사흘", 3, "일"),
    (r"나흘", 4, "일"),
    (r"닷새", 5, "일"),
    (r"엿새", 6, "일"),
    # "며칠"은 정확한 수가 없다. 규칙 단계는 recall 우선(범위를 넓게 잡아 후보를 잃지 않는 쪽)이라
    # 일주일로 본다 - 좁게 잡아 정답 문서를 빼는 비용이 넓게 잡는 비용보다 크다.
    (r"며칠", 7, "일"),
    (r"반\s*년", 6, "개월"),
    (r"한\s*해", 1, "년"),
)
_RECENT_COUNT_WORDS: dict[str, int] = {
    "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
    "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9, "십": 10,
}
# 표면형 → parse_temporal_expression이 분기하는 정규 단위.
_RECENT_UNIT_CANON: dict[str, str] = {
    "일": "일", "주": "주", "주일": "주", "달": "개월", "월": "개월", "개월": "개월", "년": "년",
}
_FUSED_ALT = "|".join(pattern for pattern, _count, _unit in _FUSED_RECENT)
# 긴 수사가 짧은 수사에 먹히지 않도록 길이 내림차순(정규식 교체는 왼쪽 우선).
_COUNT_WORD_ALT = "|".join(sorted(_RECENT_COUNT_WORDS, key=len, reverse=True))
# "최근"/"지난" 뒤에 올 수 있는 수량 구절. `search/constraints.py`가 이 표면형을 재사용한다
# (semantic_query에서 걷어내기 / "최근"을 latest 정렬 신호로 오독하지 않기) - 표기 목록이
# 두 파일로 갈라지면 반드시 어긋나므로 여기 하나만 둔다.
#
# 월 단위는 일부러 "개월|달"만 받고 **bare "월"은 받지 않는다** - 받으면 "지난 4월"/"지난 오월"
# 처럼 특정 달을 가리키는 표현이 "최근 4개월"/"최근 5개월"로 오독된다(실측: `now`=2026-09-14
# 기준 "지난 4월 회의록" → `start=2026-05-01, label='최근 4개월'`). 특정 달 표현은
# `_LAST_BARE_MONTH_RE`가 따로 받는다.
_RECENT_MONTH_UNIT = r"개월|달"
RECENT_QUANTITY_PATTERN = (
    rf"(?:{_FUSED_ALT}|\d+\s*(?:{_RECENT_MONTH_UNIT}|주일?|일|년)|(?:{_COUNT_WORD_ALT})\s*(?:{_RECENT_MONTH_UNIT}|주일?|년))"
)
RECENT_SPAN_PATTERN = rf"(?:최근|지난|저번)\s*{RECENT_QUANTITY_PATTERN}"
# "지난 일주일"도 받는다 - 달력 주("지난주", `_LAST_WEEK_RE`)와 달리 now 기준 롤링 구간이다.
# "지난주"/"지난 달"은 수량 자리가 비어 이 패턴에 안 걸리고 기존 달력 경계 분기로 내려간다.
_RECENT_RE = re.compile(
    rf"(?P<recent_marker>최근|지난|저번)\s*(?:"
    rf"(?P<fused>{_FUSED_ALT})"
    rf"|(?P<num>\d+)\s*(?P<num_unit>{_RECENT_MONTH_UNIT}|주일?|일|년)"
    rf"|(?P<word>{_COUNT_WORD_ALT})\s*(?P<word_unit>{_RECENT_MONTH_UNIT}|주일?|년)"
    rf")"
)
# 연-월-일. **`_YEAR_MONTH_RE`보다 먼저** 봐야 한다 - 안 그러면 "2026년 8월 13일"이
# 8월 한 달(08-01~09-01)로 뭉개진다. 실측(v1 골든셋): `v1-temporal-004`
# "2026년 8월 13일에 Google Drive에 올라온 문서"와 `v1-temporal-006`이 각각 ndcg 0.000이었고,
# `v1-temporal-002`("2026년 8월 12일에 받은 메일 전부")는 한 달치를 다 끌어와
# exhaustive_recall 1.000 / ndcg 0.000 - **정답을 다 담고도 10위 밖으로 밀렸다.**
#
# 일(日) 자리에 숫자를 **반드시** 요구한다. "2025년 3월에 있었던 일"의 "일"은 날짜가 아니라
# 의존명사다(기존 테스트가 이 문장을 쓴다).
_YEAR_MONTH_DAY_RE = re.compile(r"(\d{4})\s?년\s?(\d{1,2})\s?월\s?(\d{1,2})\s?일")
# ISO 날짜. 앞뒤에 숫자가 더 붙으면(전화번호/버전 문자열) 날짜가 아니다.
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s?년\s?(\d{1,2})\s?월")
# 연도 명시 분기. "2025년 Q3"/"2025 3분기"(연도가 앞)와 "Q1 2025"(분기가 앞) 둘 다 받는다 -
# 실측: "Q1 2025 보고서"가 연도-먼저 패턴만 받던 옛 정규식에서 2026년 Q1(현재 연도)로 잘못
# 해석됐다. 두 어순이 서로 다른 그룹 이름을 쓰는 이유: 파이썬 `re`는 명명 그룹 재사용을
# 허용하지 않는다(`(?|...)` 분기 재설정도 없다).
_QUARTER_WITH_YEAR_RE = re.compile(
    r"(?:(?P<y1>\d{4})\s?(?:년)?\s*(?:[Qq](?P<q1a>\d)|(?P<q1b>\d)\s?분기))"
    r"|(?:(?:[Qq](?P<q2a>\d)|(?P<q2b>\d)\s?분기)\s?(?P<y2>\d{4})\s?년?)"
)
_QUARTER_BARE_RE = re.compile(r"[Qq](\d)|(\d)\s?분기")
_YEAR_ONLY_RE = re.compile(r"(\d{4})\s?년")
# "지난 4월"/"지난 오월"처럼 연도 없이 달만 가리키는 표현 - 숫자(1~12월)와 한자어 수사 이름
# (오월=5월 등) 둘 다 받는다. 11/12월의 두 글자 수사(십일/십이)는 지원하지 않는다 - "지난
# 십일월"보다 "지난 11월"이 압도적으로 자연스러워 실익이 적다.
_KO_MONTH_WORDS: dict[str, int] = {
    "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "유": 6, "칠": 7, "팔": 8, "구": 9, "시": 10,
}
_KO_MONTH_WORD_ALT = "|".join(sorted(_KO_MONTH_WORDS, key=len, reverse=True))
# 연도 없는 "N월" 표현의 공유 몸통 - `search/constraints.py`의 semantic_query 스트리핑이
# "지난"/"저번" 접두와 묶어 재사용한다(RECENT_QUANTITY_PATTERN과 같은 이유로 한 곳에만 둔다).
BARE_MONTH_PATTERN = rf"(?:1[0-2]|0?[1-9])\s?월|(?:{_KO_MONTH_WORD_ALT})월"
_LAST_BARE_MONTH_RE = re.compile(
    rf"(?:지난|저번)\s?(?:(?P<num>1[0-2]|0?[1-9])\s?월|(?P<word>{_KO_MONTH_WORD_ALT})월)"
)
# `resolve_within_year`가 "올해 3월"/"올해 5월 15일"처럼 접두 없이 연도 문맥 아래의 달(과 날)을
# 찾을 때 쓴다. 일은 "N일"까지 적혀야 받는다 - "3월 15"처럼 숫자만 더 붙으면 날짜인지 몰라 건드리지 않는다.
#
# 한자어 수사 단독형(word)은 여기서 받지 않는다 - "이월"은 회계 용어(예산 이월/이월 잔액)와
# 완전히 동음이의라, "지난/저번" 같은 명시적 접두(그건 `_LAST_BARE_MONTH_RE`가 받는다) 없이
# 문장 아무 데서나 찾으면 왼쪽에 경계를 세워도 못 걸러진다(실측: "작년 예산 이월 내역" →
# 2025년 2월, 앞에 공백이 있어 어절 경계 자체는 이미 만족한다). 숫자형("3월")은 이런
# 동음이의가 없어 계속 받는다.
_BARE_MONTH_ONLY_RE = re.compile(r"(?<!\d)(?P<num>1[0-2]|0?[1-9])\s?월(?:\s?(?P<day>\d{1,2})\s?일)?(?!\s?\d)")


def _recent_amount(match: re.Match[str]) -> tuple[int, str] | None:
    """`_RECENT_RE` 매치를 (수, 정규 단위) 계약으로 편다 - 어느 갈래로 잡혔든 호출부는
    (count, "일"|"주"|"개월"|"년") 하나만 본다."""
    fused = match.group("fused")
    if fused is not None:
        for pattern, count, unit in _FUSED_RECENT:
            if re.fullmatch(pattern, fused):
                return count, unit
        return None
    if match.group("num") is not None:
        count, unit = int(match.group("num")), _RECENT_UNIT_CANON[match.group("num_unit")]
    else:
        count, unit = _RECENT_COUNT_WORDS[match.group("word")], _RECENT_UNIT_CANON[match.group("word_unit")]
    # "지난 12일"/"저번 3일"은 한국어에서 거의 항상 "그 달의 12일/3일"(날짜)을 가리키지, "최근
    # 12일"과 달리 기간을 뜻하지 않는다 - 실측: "지난 12일" → 최근 12일(오독). 날짜 자체를 셈할
    # 근거(해당 월의 1~12일 중 어느 달인지)가 없으므로 여기서는 해석하지 않고 그대로 둔다(다른
    # 어떤 패턴도 못 잡으면 `parse_temporal_expression`은 None을 돌려준다 - 틀리게 좁히는 것보다
    # 안전하다). 융합형("하루"/"이틀"/"며칠" 등)은 관용구라 마커와 무관하게 그대로 기간이다.
    if unit == "일" and match.group("recent_marker") != "최근":
        return None
    return count, unit


def local_date_start(now: datetime, year: int, month: int, day: int = 1) -> datetime:
    """주어진 연-월-일의 자정을 서울시(KST, UTC+9, DST 없음) 기준으로 만들어 UTC로 돌려준다 -
    이 모듈과 `search/constraints.py::_supplemental_time_range`가 만드는 모든 달력 경계가
    거치는 단 하나의 통로(모듈 docstring 참고). `now`는 tzinfo만 빌려 쓴다."""
    local = now.astimezone(SEOUL_TZ).replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


def add_months(dt: datetime, months: int) -> datetime:
    """`dt`가 가리키는 KST 달의 1일 자정에서 `months`만큼 이동한 달의 1일 자정(KST) - `day`는
    입력과 무관하게 항상 1로 리셋된다(호출부는 전부 달의 시작/끝 경계를 구하려는 것이지 같은
    날짜를 다른 달로 옮기려는 게 아니다)."""
    local = dt.astimezone(SEOUL_TZ)
    month_index = local.month - 1 + months
    year = local.year + month_index // 12
    month = month_index % 12 + 1
    return local_date_start(dt, year, month)


def _start_of_day(dt: datetime) -> datetime:
    local = dt.astimezone(SEOUL_TZ)
    return local_date_start(dt, local.year, local.month, local.day)


def _start_of_week(dt: datetime) -> datetime:
    day_start = _start_of_day(dt)
    weekday = day_start.astimezone(SEOUL_TZ).weekday()
    return day_start - timedelta(days=weekday)


def _start_of_month(dt: datetime) -> datetime:
    local = dt.astimezone(SEOUL_TZ)
    return local_date_start(dt, local.year, local.month)


def _start_of_quarter(dt: datetime) -> datetime:
    local = dt.astimezone(SEOUL_TZ)
    quarter_start_month = ((local.month - 1) // 3) * 3 + 1
    return local_date_start(dt, local.year, quarter_start_month)


def resolve_within_year(text: str, now: datetime, year: int) -> TimeRange | None:
    """이미 확정된 연도 문맥(`year`) 아래에서 분기/월 표현을 찾아 그 연도의 분기/월 범위로
    돌려준다 - `search/constraints.py::_supplemental_time_range`가 "작년"/"올해"/"재작년"/
    "지난해" 같은 연도 단어를 먼저 잡은 뒤 이 함수를 부른다(그래야 "작년 3분기"의 bare 분기가
    `parse_temporal_expression`의 현재-연도 기본값에 먼저 먹히지 않는다). 분기/월이 없으면
    None - 호출자가 연도 전체 범위로 대신한다."""
    m = _QUARTER_BARE_RE.search(text)
    if m:
        quarter = int(m.group(1) or m.group(2))
        if 1 <= quarter <= 4:
            start = local_date_start(now, year, (quarter - 1) * 3 + 1)
            return TimeRange(start, add_months(start, 3), f"{year}년 Q{quarter}")
    m = _BARE_MONTH_ONLY_RE.search(text)
    if m:
        month = int(m.group("num"))
        if m.group("day"):
            day = int(m.group("day"))
            try:
                start = local_date_start(now, year, month, day)
                return TimeRange(start, start + timedelta(days=1), f"{year}년 {month}월 {day}일")
            except ValueError:
                pass  # 없는 날(2월 30일)은 그 달로 - `parse_temporal_expression`과 같은 태도
        start = local_date_start(now, year, month)
        return TimeRange(start, add_months(start, 1), f"{year}년 {month}월")
    return None


def parse_temporal_expression(text: str, now: datetime | None = None) -> TimeRange | None:
    """Returns the first temporal expression found in `text` as an explicit
    range, or None if it doesn't contain one recognized here. Checked in
    order from most to least specific (an explicit year-month/quarter before
    the bare "이번 달"-style relative forms, which in turn come before a bare
    4-digit year) so a fully-qualified expression never gets swallowed by a
    looser pattern."""
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(SEOUL_TZ)

    # 하루짜리 범위가 가장 좁은 형태라 맨 앞이다. 연-월보다 뒤에 두면 절대 안 걸린다.
    for regex, label in ((_YEAR_MONTH_DAY_RE, "ko"), (_ISO_DATE_RE, "iso")):
        m = regex.search(text)
        if not m:
            continue
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            start = local_date_start(now, year, month, day)
        except ValueError:
            # 2월 30일 같은 없는 날짜. 범위를 만들지 않고 더 느슨한 패턴에 넘긴다 -
            # 틀린 하루로 좁히는 것보다 한 달로 넓게 잡는 편이 낫다.
            continue
        text_label = f"{year}년 {month}월 {day}일" if label == "ko" else f"{year}-{month:02d}-{day:02d}"
        return TimeRange(start, start + timedelta(days=1), text_label)

    m = _YEAR_MONTH_RE.search(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            start = local_date_start(now, year, month)
            return TimeRange(start, add_months(start, 1), f"{year}년 {month}월")

    m = _QUARTER_WITH_YEAR_RE.search(text)
    if m:
        year = int(m.group("y1") or m.group("y2"))
        quarter = int(m.group("q1a") or m.group("q1b") or m.group("q2a") or m.group("q2b"))
        if 1 <= quarter <= 4:
            start = local_date_start(now, year, (quarter - 1) * 3 + 1)
            return TimeRange(start, add_months(start, 3), f"{year}년 Q{quarter}")

    m = _LAST_BARE_MONTH_RE.search(text)
    if m:
        month = int(m.group("num")) if m.group("num") else _KO_MONTH_WORDS[m.group("word")]
        # "지난 4월"은 "가장 최근에 지나간 4월"이다 - 올해 4월이 이미 지났으면(지금이 9월이면)
        # 올해, 아직 안 왔으면(지금이 2월이면) 작년. 실측: now=2026-09-14 기준
        # "지난 4월 회의록" → 2026년 4월(이미 지난 4월이 올해에 있었으므로).
        year = local_now.year if month < local_now.month else local_now.year - 1
        start = local_date_start(now, year, month)
        return TimeRange(start, add_months(start, 1), f"{year}년 {month}월")

    m = _RECENT_RE.search(text)
    amount = _recent_amount(m) if m else None
    if amount is not None:
        count, unit = amount
        if unit == "일":
            return TimeRange(now - timedelta(days=count), None, f"최근 {count}일")
        if unit == "주":
            return TimeRange(now - timedelta(weeks=count), None, f"최근 {count}주")
        if unit == "년":
            return TimeRange(add_months(_start_of_day(now), -12 * count), None, f"최근 {count}년")
        return TimeRange(add_months(_start_of_day(now), -count), None, f"최근 {count}개월")

    if _TODAY_RE.search(text):
        start = _start_of_day(now)
        return TimeRange(start, start + timedelta(days=1), "오늘")
    if _DAY_BEFORE_YESTERDAY_RE.search(text):
        start = _start_of_day(now) - timedelta(days=2)
        return TimeRange(start, start + timedelta(days=1), "그제")
    if _YESTERDAY_RE.search(text):
        start = _start_of_day(now) - timedelta(days=1)
        return TimeRange(start, start + timedelta(days=1), "어제")
    if _LAST_WEEK_RE.search(text):
        this_week_start = _start_of_week(now)
        start = this_week_start - timedelta(days=7)
        return TimeRange(start, this_week_start, "지난주")
    if _THIS_WEEK_RE.search(text):
        start = _start_of_week(now)
        return TimeRange(start, start + timedelta(days=7), "이번주")
    if _LAST_MONTH_RE.search(text):
        this_month_start = _start_of_month(now)
        return TimeRange(add_months(this_month_start, -1), this_month_start, "지난달")
    if _THIS_MONTH_RE.search(text):
        start = _start_of_month(now)
        return TimeRange(start, add_months(start, 1), "이번달")
    if _LAST_QUARTER_RE.search(text):
        this_quarter_start = _start_of_quarter(now)
        return TimeRange(add_months(this_quarter_start, -3), this_quarter_start, "지난분기")
    if _THIS_QUARTER_RE.search(text):
        start = _start_of_quarter(now)
        return TimeRange(start, add_months(start, 3), "이번분기")

    m = _QUARTER_BARE_RE.search(text)
    if m:
        quarter = int(m.group(1) or m.group(2))
        if 1 <= quarter <= 4:
            start = local_date_start(now, local_now.year, (quarter - 1) * 3 + 1)
            return TimeRange(start, add_months(start, 3), f"{local_now.year}년 Q{quarter}")

    m = _YEAR_ONLY_RE.search(text)
    if m:
        year = int(m.group(1))
        start = local_date_start(now, year, 1)
        return TimeRange(start, local_date_start(now, year + 1, 1), f"{year}년")

    return None
