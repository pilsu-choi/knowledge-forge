"""제목 토큰 집합 매칭 - `documents.title`을 "구분자와 순서를 무시한 토큰 집합"으로 보고
질의와 맞춰 보는 순수 함수들(`PostgresStore.search_documents_by_title`이 유일한 호출자).

## 왜 필요한가

기존 제목 매칭 1단계는 **질의 전체 substring**(`title ILIKE '%q%'`)이라 순서와 구분자에
그대로 묶인다. 토큰이 전부 들어 있어도 배열이 다르면 못 맞는다:

- 질의 `"Dev Handover README"` vs 제목 `README_ProjectIntel_Dev_Handover.md`
  (토큰은 다 있는데 순서가 다르고 구분자가 `_`)
- 질의 `"codex README"` vs 제목 `codex-README.md` (구분자가 공백 vs `-`)

사람이 파일명을 기억해서 치는 방식이 원래 이렇다 - 순서와 구분자는 기억에서 가장 먼저
날아간다. 그래서 "토큰 집합이 같다/포함된다"를 substring과 같은 급의 신호로 올린다.

## 왜 임계값을 낮추는 게 답이 아닌가

같은 증상에 대한 다른 처방은 anchor 임계값(`search/retrieve.py::_ANCHOR_MIN_SCORE`) 인하다.
그건 실측으로 틀렸다: `v0-ambiguous-002`(질의 `"README"`)는 제목 substring 1.0이 두 건
(`codex-README.md`, `README_ProjectIntel_Dev_Handover.md`)이고 **둘 다 정답**이라 하나로
좁히면 안 된다. 임계값을 낮추면 그런 질의에 엉뚱한 anchor가 박힌다. 문제는 "1.0의 문턱이
높다"가 아니라 "1.0의 판정식이 순서에 묶여 있다"였다.

## 1.0을 얼마나 아껴 쓰는가

score 1.0은 `retrieve.py::_claim_anchor`가 "이 질의와 사실상 같은 제목의 문서 하나"를
확정하는 신호로 쓴다. anchor가 잡히면 다른 전략의 히트까지 그 문서로 좁혀지므로
(`_narrow_to_anchor`), 가짜 1.0 하나가 그 질의의 검색 전체를 망친다. 그래서 1.0은 두
경우에만 준다:

1. **토큰 집합 완전 일치** (질의 토큰 집합 == 제목 토큰 집합) - `"projectintel-handover-guide.md"`
   처럼 사실상 같은 이름.
2. **유일한 부분집합** (질의 토큰 집합 ⊂ 제목 토큰 집합이면서, 후보 중 그런 문서가 하나뿐).
   `"Dev Handover README"`가 여기 해당한다.

부분집합이지만 유일하지 않으면 1.0을 주지 않고 `_SUBSET_AMBIGUOUS_SCORE`(0.8)에 둔다.
0.8인 이유가 중요하다: `_ANCHOR_MIN_GAP`이 0.2라, **토큰 tier가 올려 주는 점수의 상한을
0.8로 묶으면 원래 1.0이던 1위와의 격차가 항상 0.2 이상 유지된다** - 즉 이 tier는 없던
anchor를 만들 수는 있어도(유일 판정을 통과했을 때만) 있던 anchor를 깨뜨릴 수는 없다.

부분 일치(토큰이 절반 이상 겹치지만 부분집합은 아님)는 1.0 훨씬 아래의 별도 tier다.

## 토큰화 규칙

구분자(`_` `-` `.` 공백 `/` 등)로 자르고, 그 안에서 camelCase 경계와 문자 종류 경계
(한글/영문/숫자)로 한 번 더 자른 뒤 소문자로 접는다. 쪼개진 덩어리는 **원래 덩어리도 함께**
토큰으로 남긴다(`ProjectIntel` -> `projectintel`, `project`, `intel`): 사람은 같은 이름을
`ProjectIntel`로도 `projectintel`로도 쓰기 때문에, 한쪽만 남기면 표기가 갈릴 때 못 만난다.
양쪽(질의/제목)에 같은 함수를 쓰므로 규칙은 대칭이다.

NFC 정규화는 이 단계 **이전**에 이미 끝나 있다(`text_normalize.py`, 적재/질의 양쪽) -
여기서는 그 위에 얹는다.

## 못 고치는 것

한글 질의 - 영문 제목(`"인수인계 pptx"` vs `projectintel-development-handover.pptx`)은
토큰 집합으로 풀리지 않는다. 공통 토큰이 `pptx` 하나뿐이라 부분 일치 문턱에도 못 미친다.
번역/별칭 사전이 있어야 하는 별개 문제다(wiki/search-architecture.md 참고).

## 배치가 storage/인 이유

`search/`에 두면 `storage/postgres_store.py`가 `knowledge_forge.search` 패키지를 import하게
되는데, `search/__init__.py`가 `search.service`를, `search.service`가 다시 storage를
import해서 순환이 된다. 제목을 만드는 `storage/document_title.py`와 제목을 맞추는 이 모듈이
같은 층에 있는 편이 짝도 맞는다 - 둘 다 I/O 없는 순수 함수다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# 구분자로 쪼갠 덩어리 하나. `[^\W_]`는 "단어 문자에서 밑줄만 뺀 것" - 한글/영문/숫자/한자를
# 모두 포함하고 `_` `-` `.` 공백 `/` 같은 파일명 구분자에서 끊긴다.
_RUN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# 덩어리 안을 다시 쪼개는 규칙. 순서가 곧 우선순위다.
_PIECE_RE = re.compile(
    r"[가-힣ㄱ-ㅎㅏ-ㅣ]+"  # 한글
    r"|[぀-ヿ一-鿿]+"  # 가나/한자
    r"|\d+"  # 숫자
    r"|[A-Z]+(?![a-z])"  # 연속 대문자 약어: README, PDF, ID
    r"|[A-Za-z][a-z]*"  # CamelCase 한 조각 / 소문자 단어
    r"|[^\W\d_]+"  # 그 외 문자(키릴 등)
)

# 토큰 집합 tier가 손대는 질의 길이 상한. 이보다 길면 이름이 아니라 문장이고, 문장의 토큰이
# 제목에 다 들어 있을 리 없다(`postgres_store._MAX_QUERY_TOKENS`와 같은 취지, 같은 값).
_MAX_QUERY_TOKENS = 8

# 부분집합이지만 후보가 여럿일 때의 점수. `_ANCHOR_MIN_GAP`(0.2) 때문에 0.8이 상한이다 -
# 모듈 docstring "1.0을 얼마나 아껴 쓰는가" 참고.
_SUBSET_AMBIGUOUS_SCORE = 0.8

# 부분 일치 tier: 질의 토큰의 이 비율 이상이 제목에 있고, 겹치는 토큰이 최소 이만큼일 때만.
# 한 토큰만 겹친 것을 신호로 치면 흔한 토큰(`md`, `보고서`) 하나로 아무 문서나 올라온다.
_PARTIAL_MIN_COVERAGE = 0.5
_PARTIAL_MIN_COMMON = 2
# 부분 일치 점수는 [0.5 + 0.3*0.5, 0.5 + 0.3*1.0) = [0.65, 0.8) - 부분집합 tier 바로 아래.
_PARTIAL_BASE = 0.5
_PARTIAL_RANGE = 0.3

MATCH_NONE = "none"
MATCH_PARTIAL = "partial"
MATCH_SUBSET_AMBIGUOUS = "subset_ambiguous"
MATCH_SUBSET_UNIQUE = "subset_unique"
MATCH_EXACT_SET = "exact_set"


@dataclass(frozen=True)
class TitleTokenMatch:
    """한 제목에 대한 토큰 집합 판정. `score`는 "이 tier가 주장하는 점수"라 호출자가 기존
    tier 점수와 `max()`로 합친다 - 이 tier는 점수를 올리기만 하고 내리지 않는다."""

    kind: str
    score: float
    coverage: float


_NO_MATCH = TitleTokenMatch(kind=MATCH_NONE, score=0.0, coverage=0.0)


def title_tokens(text: str) -> frozenset[str]:
    """제목/질의를 소문자 토큰 집합으로. 구분자와 순서는 사라지고, 쪼개진 덩어리는 조각과
    원본 덩어리를 **둘 다** 남긴다(`ProjectIntel` -> `{projectintel, project, intel}`).

    집합이라 중복은 사라진다 - 제목에 같은 낱말이 두 번 나오는 것은 매칭에 아무 정보도
    더하지 않는다(`README_readme.md`가 `"readme"` 질의에 두 배로 맞을 이유가 없다)."""
    tokens: set[str] = set()
    for run in _RUN_RE.findall(text):
        pieces = _PIECE_RE.findall(run)
        for piece in pieces:
            tokens.add(piece.lower())
        if len(pieces) > 1:
            tokens.add(run.lower())
    return frozenset(tokens)


def query_tokens(query: str) -> frozenset[str]:
    """질의 쪽 토큰. 토큰이 `_MAX_QUERY_TOKENS`를 넘으면 빈 집합을 돌려 토큰 tier 자체를
    끈다 - 자른 앞부분만 쓰면 "질의 토큰이 전부 제목에 있다"는 판정의 의미가 없어진다."""
    tokens = title_tokens(query)
    return frozenset() if len(tokens) > _MAX_QUERY_TOKENS else tokens


def match_titles(query: str, titles: Sequence[str | None], full_match: Sequence[bool]) -> list[TitleTokenMatch]:
    """후보 제목들을 한꺼번에 판정한다. 한 건씩이 아니라 목록으로 받는 이유는 **유일성**
    때문이다 - "질의 토큰 집합을 통째로 담은 문서가 후보 중 하나뿐"일 때만 부분집합에 1.0을
    주므로, 판정에 다른 후보가 필요하다.

    `full_match[i]`는 그 행이 토큰 tier 밖의 이유로 이미 1.0인지(제목 전체 substring 일치)를
    알려 준다. 유일성 계산에 함께 넣어야 "substring으로 1.0인 문서가 따로 있는데 부분집합
    문서까지 1.0"이 되는 일을 막는다.

    후보 목록이 잘려 있으면(호출자의 LIMIT) 유일성 판정도 그 안에서만 참이다 - 호출자가
    over-fetch로 그 위험을 줄인다(`postgres_store._TITLE_TOKEN_OVERFETCH`)."""
    q = query_tokens(query)
    if not q:
        return [_NO_MATCH] * len(titles)

    token_sets = [title_tokens(title) if title else frozenset() for title in titles]
    subset_flags = [bool(t) and q <= t for t in token_sets]
    # 1.0 자격이 있는 문서 수 - substring으로 이미 1.0인 행과 부분집합 행을 함께 센다.
    n_full = sum(1 for i in range(len(titles)) if subset_flags[i] or full_match[i])

    matches: list[TitleTokenMatch] = []
    for tokens, is_subset in zip(token_sets, subset_flags, strict=True):
        common = len(q & tokens)
        coverage = common / len(q)
        if is_subset and q == tokens:
            matches.append(TitleTokenMatch(MATCH_EXACT_SET, 1.0, 1.0))
        elif is_subset and n_full == 1:
            matches.append(TitleTokenMatch(MATCH_SUBSET_UNIQUE, 1.0, 1.0))
        elif is_subset:
            matches.append(TitleTokenMatch(MATCH_SUBSET_AMBIGUOUS, _SUBSET_AMBIGUOUS_SCORE, 1.0))
        elif common >= _PARTIAL_MIN_COMMON and coverage >= _PARTIAL_MIN_COVERAGE:
            matches.append(TitleTokenMatch(MATCH_PARTIAL, _PARTIAL_BASE + _PARTIAL_RANGE * coverage, coverage))
        else:
            matches.append(_NO_MATCH)
    return matches
