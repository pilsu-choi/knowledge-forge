"""저장/질의 문자열의 유니코드 정규화(NFC) - 어휘(lexical) 경로가 "눈에 같아 보이는 두
문자열"을 같다고 판정하게 만드는 단 한 곳.

## 왜 필요한가

한글 "관"은 코드포인트가 두 가지다: NFC의 완성형 음절 U+AD00 하나, NFD의 자모 분해
U+1100 U+1161 U+11AB 셋. 사람 눈에는 구분이 안 되지만 파이썬 `in`, SQL `ILIKE`,
pg_trgm `word_similarity`에는 전혀 다른 문자열이다.

macOS/Google Drive를 거친 파일명은 NFD로 들어온다. 그 파일명이 gmail 알림 메일 제목에
박히면 `documents.title` 한 행 안에서 **파일명 부분만 NFD, 나머지 한글은 NFC**인 혼합
정규화가 된다. 그러면 사용자가 NFC로 그 파일명을 그대로 쳐도 `title ILIKE '%q%'`가
구조적으로 못 맞아, `search_documents_by_title`의 tier 1(정확 substring, score 1.0)이
통째로 죽고 tier 3(trigram 유사도)로 떨어진다. 실제로 파일명이 정확히 일치하는 문서가
0.4286점 9위, 식별자가 다른 문서들이 0.4762점으로 그 위에 올라왔다.

## 왜 NFC이고 NFKC가 아닌가

`ingest_version.py::family_key_of`는 이미 NFKC+casefold를 쓴다. 하지만 그건 화면에
안 나오고 비교에만 쓰이는 **파생 키**라, 전각/반각·기호까지 공격적으로 접어도 잃을 게
없다. 여기서 다루는 `documents.title` / `chunks.text`는 검색 대상인 **동시에 그대로
사용자에게 보여지는 원문**이라 기준이 다르다.

- 이 버그는 "같은 문자를 다른 코드포인트 열로 적은" 정준 등가(canonical equivalence)
  문제다. NFC가 정확히 그것만 고치는 최소 수정이다.
- NFKC는 호환 문자까지 접는다: `㈜`→`(주)`, `Ⅳ`→`IV`, 전각 `ＡＢ`→`AB`, `①`→`1`,
  NBSP→공백. 국내 문서 파일명에는 `㈜`·전각 괄호·로마숫자가 실제로 흔해서, 저장값을
  NFKC로 접으면 사용자가 아는 파일명과 다른 문자열을 보여주게 된다(표시 손실).
- 저장·교환 문자열의 표준 권고도 NFC다(W3C Character Model). 적재/질의 양쪽에 같은
  NFC를 걸면 어느 쪽이 NFD로 들어와도 만난다.

호환 문자까지 접어야 하는 매칭이 나중에 필요해지면, 저장값을 바꾸는 대신 NFKC 파생
키를 따로 두는 쪽이 맞다(`family_key_of`가 그 선례다).

## 어디에 거는가

- 적재: `storage/document_title.py::_clean`(→ `derive_document_title`, 즉
  `save_document`와 `backfill_document_titles` 양쪽), `PostgresStore.save_chunks`.
- 질의: `PostgresStore.search_documents_by_title` / `search_chunks_lexical` 진입점.

한쪽만 고치면 안 맞는다 - 저장만 고치면 사용자가 NFD로 붙여넣었을 때 또 못 맞고,
질의만 고치면 이미 저장된 NFD 행에 못 맞는다.

`sources.metadata`(수집기가 API에서 받은 원본 기록)와 `documents.elements`(파서 출력)는
일부러 건드리지 않는다. 있는 그대로의 기록이라는 게 그 컬럼들의 존재 이유고, 제목 파생과
청크 저장이라는 **사용 지점**에서 정규화하면 이미 NFD로 들어와 있는 행들도 함께 낫는다.
"""

from __future__ import annotations

import unicodedata


def to_nfc(text: str) -> str:
    """문자열을 NFC로 정규화한다. 이미 NFC면 같은 문자열이 그대로 돌아온다
    (`unicodedata.normalize`는 quick-check로 빠르게 빠져나온다)."""
    return unicodedata.normalize("NFC", text)
