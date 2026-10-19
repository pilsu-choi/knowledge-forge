"""어휘 BM25(`PostgresStore.search_chunks_bm25`)용 한국어 형태소 토큰화 - 색인(청크 저장/백필)과 질의가
**같은 함수**를 거쳐야 한다. 조사·어미가 붙은 어절("전략을", "전략은")이 같은 term("전략")이 되는 것이 요점이다.

Kiwi(`kiwipiepy`, `korean` extra)는 첫 호출에 모델을 올린다(~0.5s). 모듈 전역 하나를 재사용한다.
토큰은 내용어(명사·어근·용언 어간)와 외국어/숫자/한자만 남기고 소문자로 쓴다 - 조사·어미·기호는 변별력이 없다.
`ERR_CONNECTION_RESET` 같은 식별자는 `ERR`, `CONNECTION`, `RESET`으로 갈라지는데, 식별자 전체 일치는
pg_trgm substring 층(`search_chunks_lexical`)의 몫이다.
"""

from __future__ import annotations

import threading
from typing import Any

from knowledge_forge.text_normalize import to_nfc

# Kiwi 품사 태그 중 남기는 것: 일반/고유명사, 수사, 어근, 동사/형용사 어간, 외국어(SL), 숫자(SN), 한자(SH).
# 의존명사(NNB: "수", "것", "시")는 변별력이 없어 뺀다.
_KEEP_TAGS = frozenset({"NNG", "NNP", "NR", "XR", "VV", "VA", "SL", "SN", "SH"})

_kiwi: Any = None
_lock = threading.Lock()


def _get_kiwi() -> Any:
    global _kiwi
    if _kiwi is None:
        with _lock:
            if _kiwi is None:
                try:
                    from kiwipiepy import Kiwi
                except ImportError as exc:
                    raise RuntimeError(
                        "한국어 BM25에는 kiwipiepy가 필요합니다: pip install 'knowledge-forge[korean]'"
                    ) from exc
                _kiwi = Kiwi()
    return _kiwi


def tokenize(text: str) -> list[str]:
    """`text`의 BM25 term 목록(순서·중복 유지 - tf 계산용)."""
    tokens = _get_kiwi().tokenize(to_nfc(text))
    return [t.form.lower() for t in tokens if t.tag in _KEEP_TAGS]
