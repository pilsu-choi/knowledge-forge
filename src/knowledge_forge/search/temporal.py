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

All boundaries are computed in UTC (`datetime.now(timezone.utc)` by
default) because every timestamp this system stores (`Source.collected_at`/
`modified_at`, `KnowledgeObject.extracted_at`) is UTC - this keeps the
filter's timezone assumption consistent with the data it filters, but does
mean "오늘"/"어제" are UTC calendar days, not the caller's local calendar day
(a real limitation for a caller far from UTC, e.g. KST is UTC+9 - "어제"
near UTC midnight can be off by up to a day from that caller's intuition).
Monday is the start of the week (ISO), matching common Korean convention.

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
RECENT_QUANTITY_PATTERN = (
    rf"(?:{_FUSED_ALT}|\d+\s*(?:개?월|달|주일?|일|년)|(?:{_COUNT_WORD_ALT})\s*(?:개?월|달|주일?|년))"
)
RECENT_SPAN_PATTERN = rf"(?:최근|지난|저번)\s*{RECENT_QUANTITY_PATTERN}"
# "지난 일주일"도 받는다 - 달력 주("지난주", `_LAST_WEEK_RE`)와 달리 now 기준 롤링 구간이다.
# "지난주"/"지난 달"은 수량 자리가 비어 이 패턴에 안 걸리고 기존 달력 경계 분기로 내려간다.
_RECENT_RE = re.compile(
    rf"(?:최근|지난|저번)\s*(?:"
    rf"(?P<fused>{_FUSED_ALT})"
    rf"|(?P<num>\d+)\s*(?P<num_unit>개?월|달|주일?|일|년)"
    rf"|(?P<word>{_COUNT_WORD_ALT})\s*(?P<word_unit>개?월|달|주일?|년)"
    rf")"
)
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s?년\s?(\d{1,2})\s?월")
# Year-qualified quarter, tried before the bare form below so "2025 Q3"/
# "2025년 3분기" keep their explicit year instead of defaulting to `now.year`.
_QUARTER_WITH_YEAR_RE = re.compile(r"(\d{4})\s?(?:년)?\s*(?:[Qq](\d)|(\d)\s?분기)")
_QUARTER_BARE_RE = re.compile(r"[Qq](\d)|(\d)\s?분기")
_YEAR_ONLY_RE = re.compile(r"(\d{4})\s?년")


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
        return int(match.group("num")), _RECENT_UNIT_CANON[match.group("num_unit")]
    return _RECENT_COUNT_WORDS[match.group("word")], _RECENT_UNIT_CANON[match.group("word_unit")]


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_week(dt: datetime) -> datetime:
    day_start = _start_of_day(dt)
    return day_start - timedelta(days=day_start.weekday())


def _start_of_month(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _start_of_quarter(dt: datetime) -> datetime:
    quarter_start_month = ((dt.month - 1) // 3) * 3 + 1
    return dt.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    return dt.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def parse_temporal_expression(text: str, now: datetime | None = None) -> TimeRange | None:
    """Returns the first temporal expression found in `text` as an explicit
    range, or None if it doesn't contain one recognized here. Checked in
    order from most to least specific (an explicit year-month/quarter before
    the bare "이번 달"-style relative forms, which in turn come before a bare
    4-digit year) so a fully-qualified expression never gets swallowed by a
    looser pattern."""
    now = now or datetime.now(timezone.utc)

    m = _YEAR_MONTH_RE.search(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            start = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
            return TimeRange(start, _add_months(start, 1), f"{year}년 {month}월")

    m = _QUARTER_WITH_YEAR_RE.search(text)
    if m:
        year = int(m.group(1))
        quarter = int(m.group(2) or m.group(3))
        if 1 <= quarter <= 4:
            start = now.replace(year=year, month=(quarter - 1) * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return TimeRange(start, _add_months(start, 3), f"{year}년 Q{quarter}")

    m = _RECENT_RE.search(text)
    amount = _recent_amount(m) if m else None
    if amount is not None:
        count, unit = amount
        if unit == "일":
            return TimeRange(now - timedelta(days=count), None, f"최근 {count}일")
        if unit == "주":
            return TimeRange(now - timedelta(weeks=count), None, f"최근 {count}주")
        if unit == "년":
            return TimeRange(_add_months(_start_of_day(now), -12 * count), None, f"최근 {count}년")
        return TimeRange(_add_months(_start_of_day(now), -count), None, f"최근 {count}개월")

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
        return TimeRange(_add_months(this_month_start, -1), this_month_start, "지난달")
    if _THIS_MONTH_RE.search(text):
        start = _start_of_month(now)
        return TimeRange(start, _add_months(start, 1), "이번달")
    if _LAST_QUARTER_RE.search(text):
        this_quarter_start = _start_of_quarter(now)
        return TimeRange(_add_months(this_quarter_start, -3), this_quarter_start, "지난분기")
    if _THIS_QUARTER_RE.search(text):
        start = _start_of_quarter(now)
        return TimeRange(start, _add_months(start, 3), "이번분기")

    m = _QUARTER_BARE_RE.search(text)
    if m:
        quarter = int(m.group(1) or m.group(2))
        if 1 <= quarter <= 4:
            start = now.replace(month=(quarter - 1) * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return TimeRange(start, _add_months(start, 3), f"{now.year}년 Q{quarter}")

    m = _YEAR_ONLY_RE.search(text)
    if m:
        year = int(m.group(1))
        start = now.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return TimeRange(start, start.replace(year=year + 1), f"{year}년")

    return None
