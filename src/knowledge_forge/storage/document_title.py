"""Derives `documents.title` (storage/schema.sql) - the one string
`search/service.py::document_search` matches a "2025_SH_업무_보고서 찾아줘"
style navigational query against.

Why this exists at all: `Document.metadata.title` is what a parser managed
to read out of the file itself, and for everything the SaaS collectors
bring in that is NULL - a Drive PDF's parser sees bytes, not the Drive file
name; a Gmail thread's parser sees a rendered body, not the subject line.
The name a *person* knows the document by lives in `sources.metadata`
(`name` for google_drive, `subject` for gmail, `channel` for slack), so the
title has to be assembled from both rows. Doing it once at write time (and
`PostgresStore.backfill_document_titles` for rows written before the column
existed) keeps the search query a plain indexed column match instead of a
CASE over two JSONB blobs.

Pure functions, no I/O - `PostgresStore.save_document` feeds it the model,
the backfill feeds it raw row values; both go through `derive_document_title`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from knowledge_forge.text_normalize import to_nfc

# Where a title lives in `sources.metadata`, per collector (see
# collectors/saas/*.py) - checked in order, first non-empty wins. `name` is
# also what a local_file/notion collector would most naturally use, so it
# is tried for every source type rather than keyed by type.
_SOURCE_TITLE_KEYS = ("title", "name", "subject", "file_name", "filename")

# Slack 스레드에는 사람이 붙인 제목이 없다. 그래서 제목을 "#<채널명> <YYYY-MM-DD>"로 조립하고,
# `_SLACK_EXCERPT_CHARS`를 0보다 크게 두면 뒤에 첫 메시지 발췌를 덧붙인다.
#
# 왜 채널 **ID**가 아니라 채널명인가: 개발 DB의 슬랙 문서 197건은 전부 제목이
# "#C0BGQEQG1L6 2026-09-01"이었다. 채널 ID는 사람이 질의에 쓰지 않는 문자열이라 제목 검색이
# 구조적으로 못 맞고, 후보 목록을 읽는 에이전트(`search/planner.py`가 hit["title"]을 본다)에게도
# 아무것도 알려주지 않는다. 채널명은 이 코퍼스에서 `proj_2026-038-a_nh농협은행_...`처럼
# 프로젝트 코드까지 담고 있다.
#
# 발췌를 왜 기본으로 켜지 않는가 - 전부 실측이다(v0 골든셋 76질의, 개발 DB 358문서).
#
# (1) 제목 검색 경로만 떼어 본 오프라인 측정(3-tier 재현, 슬랙 정답 66건 recall@10):
#       "#<채널ID> <날짜>"                    0/66
#       "#<채널명> <날짜>"                    0/66     <- 채널명만으로는 아무것도 안 낫는다
#       + 첫 메시지 60자                     34/66
#       + 첫 메시지 120자                    42/66
#       + 첫 메시지 200자                    42/66     <- 120자에서 포화
#     사람은 슬랙을 찾을 때 채널 이름을 대지 않는다. "caddy-drift 아웃테이지 알림",
#     "OCR 제품 교육 참석 여부를 묻는 스레드"처럼 **첫 메시지 내용**을 댄다.
#
# (2) 그런데 파이프라인 전체(eval-ir v0, --no-llm)로는 셋 다 노이즈 안에서 구분되지 않는다:
#       채널ID(기준)          ndcg@10 0.8516   run da5cf39a0cc64bcf9576f020c6f00e26
#       채널명                        0.8490   run b77b9e65eda1400184bd48bd6943ae11  (0승 1패)
#       채널명+발췌120자              0.8498   run 62966bd1af93469eac681c02d6d3ffbd  (3승 5패)
#     제목 경로가 못 닿던 슬랙 문서들에 **벡터/청크 어휘 경로가 이미 닿고 있었다**는 뜻이다.
#
# (3) 그리고 발췌는 다른 곳의 잠복 결함을 깨운다. `search/retrieve.py::_metadata_filter_where`가
#     문서 종류 제약을 `d.title ILIKE '%가이드%'`로 거는데, 이 술어는 제목이 **이름**이라고
#     가정한다. 본문 발췌가 제목에 들어오면 "운영 매뉴얼 초안"을 첫 줄에 쓴 슬랙 스레드가
#     `%매뉴얼%`에 걸리고, `_is_source_hint_only_filter`가 그 필터를 "믿을 수 있는 필터"로 쳐서
#     교집합을 잡는 바람에 나머지 후보가 통째로 날아간다. 실제 피해:
#       v0-whole_document_summary-003  0.631 -> 0.000 (슬랙 스레드가 1위를 먹고 단독 생존)
#       v0-negative-001("Kubernetes 클러스터 구성 가이드")  none_correct 1.0 -> 0.0
#
# **위 (3)의 조건이 충족되어 발췌를 켰다(120).** `title_types` 술어(`d.title ILIKE '%종류%'`)는
# 커밋 09296fb에서 제거됐다 - 문서 종류를 제목으로 판정할 근거가 아예 없었기 때문이다
# (`documents.metadata`에 `document_type` 키가 없고, `knowledge_objects` 쪽은 LLM 자유
# 문자열이라 5107행에 571가지 값이며 KO가 붙은 문서도 263/358뿐이다). 그 술어를 쓰던 질의
# 4건 중 둘이 위 붕괴의 당사자였고, 제거 후 v0 delta는 0.000(0승 0패 70무)이었다.
# 재적재는 필요 없었다 - 발췌의 원재료(`sources.metadata.first_message`)가 이미 저장돼 있다.
# 자세한 표와 질의별 증감은 wiki/search-evaluation.md.
_SLACK_CHANNEL_KEYS = ("channel_name", "channel")
_SLACK_EXCERPT_CHARS = 120  # 위 (3)의 title_types 술어가 제거되어(09296fb) 켤 수 있게 됐다

# 발췌에서 걷어내는 Slack 마크업. 검색이 맞출 수 없는 문자열이라 자릿수만 잡아먹는다.
_SLACK_EMOJI_RE = re.compile(r":[a-z0-9_+\-]+:")
_SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")
_SLACK_SPEAKER_RE = re.compile(r"^U[A-Z0-9]{6,}:\s*")

_MAX_TITLE_CHARS = 300


def slack_excerpt(first_message: Any, limit: int = _SLACK_EXCERPT_CHARS) -> str | None:
    """슬랙 에피소드 첫 메시지에서 제목에 실을 발췌를 만든다.

    - 맨 앞 `U0BDNDBHWLV: ` 화자 접두어를 뗀다. 그 접두어는 `context/slack.py::_format_message`가
      transcript를 만들 때 붙이는 것이라, `documents.elements`에서 첫 텍스트 블록을 읽어 오는
      백필 경로에는 항상 붙어 있다(수집기 경로의 `first_message`에는 없다). 양쪽을 같은
      함수로 통과시키려고 여기서 지운다.
    - `<url|label>`은 label(없으면 url)만 남기고, `:pushpin:` 같은 이모지 코드는 지운다.
    - 공백을 접고 `limit`(기본 `_SLACK_EXCERPT_CHARS`)로 자른다. 자르기는 NFC 정규화 뒤(`_clean`)가
      아니라 여기서 하지만, 최종 길이 제한은 `_clean`이 다시 건다. `limit`이 0이면 빈 발췌라
      호출부가 발췌를 붙이지 않는다 - 켜고 끄는 판단은 `_SLACK_EXCERPT_CHARS` 한 곳에만 있다."""
    if not isinstance(first_message, str):
        return None
    text = _SLACK_SPEAKER_RE.sub("", first_message)
    text = _SLACK_LINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    text = _SLACK_EMOJI_RE.sub(" ", text)
    text = " ".join(text.split())
    return text[:limit] or None


def _clean(value: Any) -> str | None:
    """공백 정리 + NFC 정규화 + 길이 제한. NFC가 여기 있는 이유: 제목의 원재료인
    `sources.metadata`의 Drive `name` / Gmail `subject`가 macOS·Google Drive를
    거치며 한글 자모가 분해된 NFD로 들어온다(현재 dev DB 81/502행). 그대로 저장하면
    `search_documents_by_title`의 substring tier가 NFC 질의에 구조적으로 못 맞는다.
    `derive_document_title`의 모든 출구가 이 함수를 지나므로 `save_document`와
    `backfill_document_titles`가 같은 규칙을 공유한다. NFC를 고른 이유는
    `text_normalize.py` 참고. 자르기는 정규화 뒤에 한다 - NFD 3코드포인트가 NFC
    1글자로 합쳐지므로 순서가 바뀌면 자르는 위치가 달라진다."""
    if not isinstance(value, str):
        return None
    text = to_nfc(" ".join(value.split()))
    if not text:
        return None
    return text[:_MAX_TITLE_CHARS]


def first_heading_text(elements: Iterable[Any]) -> str | None:
    """The text of the first `heading` element, from either parsed
    `DocumentElement` models or their JSONB dict form (what a backfill reads
    back from `documents.elements`) - the last-resort title when neither the
    parser nor the source metadata had one."""
    for element in elements:
        if isinstance(element, dict):
            kind, text = element.get("type"), element.get("text")
        else:
            kind, text = getattr(element, "type", None), getattr(element, "text", None)
        if kind == "heading":
            return _clean(text)
    return None


def derive_document_title(
    *,
    metadata_title: str | None,
    source_type: str | None,
    source_metadata: dict[str, Any] | None,
    content_at: datetime | None,
    first_heading: str | None,
) -> str | None:
    """Priority: parser-supplied `metadata_title` -> source `name`/`subject`
    -> slack `#channel YYYY-MM-DD` -> first heading -> None.

    The parser title goes first because when it exists it was authored into
    the document (a DOCX core-properties title, an HTML <title>) and is more
    specific than a file name; the source name/subject comes next because
    it is the name the user actually typed into a "find X" request; the
    heading is last because a heading is a guess at a title, not a title
    (the Slack/Gmail context parsers emit synthetic headings like
    "Slack #C0BG… 스레드" that only the pipeline ever chose)."""
    title = _clean(metadata_title)
    if title:
        return title

    metadata = source_metadata or {}
    for key in _SOURCE_TITLE_KEYS:
        title = _clean(metadata.get(key))
        if title:
            return title

    if source_type == "slack":
        channel = None
        for key in _SLACK_CHANNEL_KEYS:
            channel = _clean(metadata.get(key))
            if channel:
                break
        excerpt = slack_excerpt(metadata.get("first_message"), _SLACK_EXCERPT_CHARS)
        if channel or excerpt:
            parts = [f"#{channel.lstrip('#')}"] if channel else []
            if content_at is not None:
                parts.append(content_at.date().isoformat())
            if excerpt:
                parts.append(excerpt)
            return _clean(" ".join(parts))

    return _clean(first_heading)
