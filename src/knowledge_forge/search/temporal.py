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
_RECENT_RE = re.compile(r"최근\s*(\d+)\s*(일|주|개?월)")
_YEAR_MONTH_RE = re.compile(r"(\d{4})\s?년\s?(\d{1,2})\s?월")
# Year-qualified quarter, tried before the bare form below so "2025 Q3"/
# "2025년 3분기" keep their explicit year instead of defaulting to `now.year`.
_QUARTER_WITH_YEAR_RE = re.compile(r"(\d{4})\s?(?:년)?\s*(?:[Qq](\d)|(\d)\s?분기)")
_QUARTER_BARE_RE = re.compile(r"[Qq](\d)|(\d)\s?분기")
_YEAR_ONLY_RE = re.compile(r"(\d{4})\s?년")


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
    if m:
        count, unit = int(m.group(1)), m.group(2)
        if unit == "일":
            return TimeRange(now - timedelta(days=count), None, f"최근 {count}일")
        if unit == "주":
            return TimeRange(now - timedelta(weeks=count), None, f"최근 {count}주")
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
