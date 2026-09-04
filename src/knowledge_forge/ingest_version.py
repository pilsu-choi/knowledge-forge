"""Rule-based document version parsing from a filename or title.

"최종본 보여줘" / "3차 수정본은?" / "이 문서 이전 버전이랑 뭐가 달라?" are
questions retrieval can't answer at query time from content alone - two
revisions of the same report share almost every sentence. The version
signal lives in the *name* people give the file (`_v2`, `(1)`, `3차
수정본`, `최종`, `_260824`), so it has to be extracted at ingest and stored
on `documents.metadata` (`DocumentMetadata.version_*`) where
`search/sql.py` and `document_links` (PREVIOUS_VERSION edges, see
document_links.py) can use it.

Deliberately regex-only, no LLM: the vocabulary is small and stable, an
LLM call per document would cost more than the whole parse stage, and -
most importantly - `family_key` must be a *deterministic* function of the
name so that two files ingested months apart land in the same family.
The patterns below were chosen against the real Google Drive file names in
the dev database (see wiki/storage.md "문서 버전 메타데이터" for the
distribution and the false positives that shaped each rule).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# A trailing extension. 1-5 alphanumerics keeps `ver12.0` / `v3.1` intact
# (their "extension" would be all digits, excluded) while still catching
# `.xlsx`, `.hwpx`, `.jpeg`, `.md`.
_EXTENSION_RE = re.compile(r"\.(?![0-9]+$)[a-z0-9]{1,5}$", re.IGNORECASE)

# Reply/forward prefixes on an email subject - "Re: 보고서 v2" and
# "보고서 v2" are the same family.
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|회신|전달|답장)\s*:\s*)+", re.IGNORECASE)

# Token boundary: a version marker must not be glued to another
# alphanumeric/Hangul run on either side, or `X70M`/`CVE`/`최종점검표`-style
# substrings would match.
_LB = r"(?<![a-z0-9가-힣])"
_RB = r"(?![a-z0-9가-힣])"

# v1 / V2.1 / ver12.0 / version 3 / v3_1 (underscore as a minor separator
# is common in Korean file names: `seah_execution_plan_v3_1`).
_V_RE = re.compile(_LB + r"v(?:er(?:sion)?)?[ ._-]?(\d+(?:[._]\d+)*)" + _RB, re.IGNORECASE)
# r1 / R2 / rev3 / rev.4
_REV_RE = re.compile(_LB + r"(?:rev|r)[ ._-]?(\d+)" + _RB, re.IGNORECASE)
# "3차 수정본" / "2차 개정" / "보고서_3차" (trailing). A bare "N차" followed
# by other words ("3차 인터뷰 회의자료") is an ordinal, not a revision, so
# it only counts when qualified or at the very end of the stem.
_ORDINAL_RE = re.compile(r"(?<!\d)(\d+)\s*차(?:\s*(?:수정본|수정|개정본|개정|버전|본))" + _RB)
_ORDINAL_TRAILING_RE = re.compile(r"(?<!\d)(\d+)\s*차\s*$")
# "(1)" / "(2)" copy suffix Drive/Finder append on a name collision - only
# at the very end, and only 1-2 digits so "(2021)" in a title stays a year.
_COPY_RE = re.compile(r"\((\d{1,2})\)\s*$")
_FINAL_RE = re.compile(_LB + r"(?:final|최종본|최종|확정본|확정|완료본|latest|최신본|최신)" + _RB, re.IGNORECASE)
_DRAFT_RE = re.compile(_LB + r"(?:draft|초안|원본|original|orig)" + _RB, re.IGNORECASE)
_REVISED_RE = re.compile(_LB + r"(?:수정본|수정|개정본|개정|revised)" + _RB, re.IGNORECASE)

# Date stamps - YYYYMMDD / YYYY.MM.DD / YYYY-MM-DD / YYMMDD / YY.MM.DD.
# Month/day ranges are validated so a 6-8 digit id (`0005`, `183948`) can't
# pass as a date.
_MM = r"(0[1-9]|1[0-2])"
_DD = r"(0[1-9]|[12]\d|3[01])"
_DATE_RE = re.compile(
    r"(?<!\d)(?:"
    r"(20\d{2})([.\-]?)" + _MM + r"\2" + _DD +
    r"|(\d{2})([.\-]?)" + _MM + r"\6" + _DD +
    r")(?!\d)"
)
# Trailing = nothing but separators after it, once the other markers have
# been blanked out ("검토_260824_R1" -> "검토_260824_  ").
_DATE_TRAILING_RE = re.compile(_DATE_RE.pattern + r"[\s_\-.()\[\]]*$")

# Anything that is punctuation/whitespace between name tokens.
_SEPARATOR_RE = re.compile(r"[\s_\-.()\[\]{}·,;:'\"‘’“”~!#$%^&*+=|/\\<>?`]+")


@dataclass(frozen=True)
class VersionInfo:
    """What `parse_version` found in one name.

    label       - the marker text as written ("v2.1", "3차 수정본", "최종",
                  "(1)", or a normalized "YYYYMMDD" when a trailing date is
                  the only marker); None when the name carries no marker.
    number      - numeric position within the family (v2.1 -> 2.1, "3차" ->
                  3.0, "(1)" -> 1.0, 초안/원본 -> 0.0). None for markers with
                  no number (최종, 수정본, a bare date).
    is_final    - 최종/final/확정/latest present.
    family_key  - the name with every marker, the extension and separator
                  noise removed; identical across versions of one document.
    date_stamp  - "YYYYMMDD" when the name ends in a date (after the other
                  markers are removed) or a `v<date>` marker; a secondary
                  ordering key, since two `_260824`/`_260901` revisions of
                  a file rarely carry a `vN` as well.
    """

    label: str | None
    number: float | None
    is_final: bool
    family_key: str
    date_stamp: str | None = None


def _normalize_date(match: re.Match[str]) -> str:
    if match.group(1):
        return match.group(1) + match.group(3) + match.group(4)
    return "20" + match.group(5) + match.group(7) + match.group(8)


def _to_number(digits: str) -> float:
    """`3` -> 3.0, `2.1` -> 2.1, `3_1` -> 3.1, `1.1.2` -> 1.1 (third
    component and beyond dropped - float ordering only has one dot)."""
    parts = re.split(r"[._]", digits)
    return float(parts[0] + ("." + parts[1] if len(parts) > 1 else ""))


def _looks_like_date(digits: str) -> str | None:
    if len(digits) in (6, 8) and digits.isdigit():
        m = _DATE_RE.fullmatch(digits)
        if m is not None:
            return _normalize_date(m)
    return None


def strip_extension(name: str) -> str:
    return _EXTENSION_RE.sub("", name.strip())


def family_key_of(stem: str) -> str:
    """Normalizes an already marker-free stem into a family key: NFKC,
    lowercase, every separator run collapsed to one space."""
    text = unicodedata.normalize("NFKC", stem).casefold()
    return _SEPARATOR_RE.sub(" ", text).strip()


def parse_version(name: str | None) -> VersionInfo | None:
    """Returns None for an empty name; otherwise always a VersionInfo (an
    unmarked name is still a family member - `family_key` set, everything
    else None/False - so an unversioned original can be linked to its
    later `_v2`)."""
    if name is None or not name.strip():
        return None
    stem = strip_extension(_SUBJECT_PREFIX_RE.sub("", name))

    # Every explicit marker is collected with its position; the one nearest
    # the end wins for label/number ("Simulation_V2 패치 검토_R1" is revision
    # 1 of the V2-patch review, not version 2), but *all* are removed from
    # the family key so the V2/V3 patch reviews still group together.
    candidates: list[tuple[int, str, float | None, str | None]] = []  # (pos, label, number, date)
    spans: list[tuple[int, int]] = []

    for m in _V_RE.finditer(stem):
        digits = m.group(1)
        date = _looks_like_date(digits.replace(".", "").replace("_", "")) if "." not in digits else None
        candidates.append((m.start(), m.group(0).strip(" ._-"), None if date else _to_number(digits), date))
        spans.append(m.span())
    for m in _REV_RE.finditer(stem):
        candidates.append((m.start(), m.group(0).strip(" ._-"), float(m.group(1)), None))
        spans.append(m.span())
    for regex in (_ORDINAL_RE, _ORDINAL_TRAILING_RE):
        for m in regex.finditer(stem):
            candidates.append((m.start(), m.group(0).strip(), float(m.group(1)), None))
            spans.append(m.span())
    m_copy = _COPY_RE.search(stem)
    if m_copy is not None:
        candidates.append((m_copy.start(), m_copy.group(0).strip(), float(m_copy.group(1)), None))
        spans.append(m_copy.span())

    # Word markers are searched on what's left after the numeric ones are
    # blanked, so the "수정본" inside "3차 수정본" isn't counted twice.
    remainder = _blank_spans(stem, spans)
    is_final = False
    final_labels: list[tuple[int, str]] = []
    for m in _FINAL_RE.finditer(remainder):
        is_final = True
        final_labels.append((m.start(), m.group(0)))
        spans.append(m.span())
    draft_labels: list[tuple[int, str]] = []
    for m in _DRAFT_RE.finditer(remainder):
        draft_labels.append((m.start(), m.group(0)))
        spans.append(m.span())
    revised_labels: list[tuple[int, str]] = []
    for m in _REVISED_RE.finditer(remainder):
        revised_labels.append((m.start(), m.group(0)))
        spans.append(m.span())

    remainder = _blank_spans(stem, spans)
    date_stamp: str | None = None
    m_date = _DATE_TRAILING_RE.search(remainder)
    if m_date is not None:
        date_stamp = _normalize_date(m_date)
        remainder = remainder[: m_date.start()]
    for _, _, _, date in candidates:
        if date is not None:
            date_stamp = date_stamp or date

    label: str | None = None
    number: float | None = None
    if candidates:
        _, label, number, _ = max(candidates, key=lambda c: c[0])
    # Word markers compose with a number ("최종_v3", "v2_수정본"); the label
    # keeps both halves so a human reading `version_label` sees what the
    # file was actually called.
    word_labels = [lbl for _, lbl in sorted(final_labels + draft_labels + revised_labels)]
    if word_labels:
        label = "_".join(([label] if label else []) + word_labels)
    if number is None and draft_labels and not final_labels and not candidates:
        number = 0.0
    if label is None and date_stamp is not None:
        label = date_stamp

    return VersionInfo(
        label=label,
        number=number,
        is_final=is_final,
        family_key=family_key_of(remainder),
        date_stamp=date_stamp,
    )


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def version_sort_key(
    label: str | None,
    number: float | None,
    is_final: bool | None,
    date_stamp: str | None = None,
) -> tuple[int, int, float, str, str]:
    """Orders members of one family oldest -> newest from their stored
    `version_*` metadata (so it works on rows read back from Postgres, not
    only on a fresh `VersionInfo`). Ranking, lowest first:

      stage 0  초안/원본/draft            - explicitly "before everything"
      stage 1  no marker at all           - the original upload
      stage 2  numbered / dated           - vN, N차, (N), YYYYMMDD, by number then date
      stage 3  수정/수정본 without a number - "the edited one" of the above

    then finals last, whatever their number ("최종" beats "v3"; "v3_최종"
    beats "최종"). A numbered 수정본 ("v2_수정본") lands just after its
    number, before the next one. Ties fall through to the caller
    (documents.created_at)."""
    label = label or ""
    revised = _REVISED_RE.search(label) is not None
    if _DRAFT_RE.search(label) and number in (None, 0.0):
        stage = 0
    elif not label:
        stage = 1
    elif number is not None or date_stamp is not None or _DATE_RE.fullmatch(label):
        stage = 2
    elif revised:
        stage = 3
    else:
        stage = 2
    number_key = (number if number is not None else -1.0) + (0.001 if revised else 0.0)
    date_key = date_stamp or (label if _DATE_RE.fullmatch(label) else "")
    return (1 if is_final else 0, stage, number_key, date_key, label)
