"""검색 질의 임베딩용 유한 LRU 캐시.

왜 필요한가: `search/service.py::vector_search`는 질의 하나를 임베딩하려고 매번 원격
왕복을 한 번 한다. 이 배포는 OpenRouter를 거쳐 OpenAI로 가는 2단 홉이라 실측 **약 300ms**
(`vector_search` 1회 = 임베딩 + ANN 조회 357ms 중 ANN은 한 자릿수 ms)이고, 골든셋 76질의
기준 p50 602ms의 절반이다. 같은 문자열을 두 번 임베딩하면 그 300ms를 그대로 또 낸다.

**왜 provider가 아니라 이 계층인가** (`vector_search`가 이걸 쓰고, `EmbeddingProvider.embed`는
건드리지 않는다):

- `embed()`의 호출자는 두 종류다. (1) 적재(`pipeline.py`/`knowledge-forge embed`)는 문서
  청크 수백 개를 **한 배치**로 넘긴다 - 텍스트가 길고, 같은 텍스트가 다시 오는 일은
  재적재뿐이라 캐시 적중률이 사실상 0이면서 캐시를 통째로 밀어낸다. (2) 검색은 짧은 질의
  **한 개**를 넘기고, 그게 반복된다(평가 스윕, REPL 후속 질문, 한 턴에서 에이전트가 search를
  두 번 부르는 경우). 캐시가 이득인 건 (2)뿐이라 (2)의 진입점에 둔다.
- `get_embedding_provider()`는 호출마다 새 인스턴스를 만든다. 그래서 provider 인스턴스
  필드에 캐시를 달면 절대 적중하지 않고, 모듈 전역에 달면 위의 (1)까지 같이 걸린다.

캐시 키는 `(embedding_model, text)`다. 모델 이름을 키에 넣는 건 필수다 - 모델이 다르면
벡터 공간 자체가 다르고, `knowledge_object_embeddings`도 `embedding_model`로 파티션되어
있어서(`PostgresStore.search_by_embedding`의 docstring: "이 이름은 사실상 파티션 키다")
모델 A의 벡터로 모델 B의 행을 조회하면 에러 없이 조용히 엉뚱한 결과가 나온다.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path


class BoundedEmbeddingCache:
    """`(model, text) -> vector` LRU. 상한이 있는 dict라는 것 말고는 아무것도 아니다.

    스레드/태스크 락은 없다. asyncio 단일 이벤트 루프 안에서 이 클래스의 메서드는 await
    지점이 없어 원자적이고, 동시에 뜬 같은 질의 두 개가 둘 다 miss 나서 임베딩을 두 번
    부르는 것은 (락으로 막을 수는 있지만) 늦게 온 쪽이 먼저 온 쪽 결과를 덮어쓸 뿐이라
    정합성 문제가 아니다 - 그 한 번을 아끼려고 in-flight 관리를 넣을 만한 가치는 없다.
    """

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    def get(self, model: str, text: str) -> list[float] | None:
        key = (model, text)
        vector = self._entries.get(key)
        if vector is None:
            return None
        self._entries.move_to_end(key)
        # 복사본을 돌려준다 - 호출자(`store.search_by_embedding`)가 리스트를 그대로 쿼리
        # 파라미터로 넘기는 지금은 변형이 없지만, 캐시가 내부 상태를 밖으로 새게 두면
        # 나중에 누가 in-place로 정규화하는 순간 조용히 오염된다.
        return list(vector)

    def put(self, model: str, text: str, vector: list[float]) -> None:
        key = (model, text)
        self._entries[key] = list(vector)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# 256개. 1536차원 float 리스트 하나가 파이썬 객체 오버헤드까지 합쳐 대략 50KB라
# 상한이 ~12MB - 프로세스 하나가 들고 있어도 무해한 수준이면서, 평가 스윕(76질의)이나
# 대화 한 세션의 반복 질의는 통째로 담긴다. 늘리려면 이 숫자만 올리면 된다.
QUERY_EMBEDDING_CACHE_MAX_ENTRIES = 256

query_embedding_cache = BoundedEmbeddingCache(QUERY_EMBEDDING_CACHE_MAX_ENTRIES)


class PinnedEmbeddingCache(BoundedEmbeddingCache):
    """파일에 저장되는 무제한 `(model, text) -> vector` 캐시 - 평가가 질의 임베딩을 고정·재생하는 데 쓴다.

    원격 임베딩은 같은 문장에도 벡터가 조금씩 달라서(골든 562질의 두 번 실행에 137건 결과 차이) 작은
    delta의 A/B가 노이즈에 묻힌다. 파일이 있으면 읽어 재생하고, 없는 텍스트만 원격에서 받아 `put`으로
    쌓이며 `save()`가 파일에 되돌려 쓴다. 키에 모델이 있어 다른 모델의 벡터가 섞이지 않는다."""

    def __init__(self, path: Path) -> None:
        super().__init__(max_entries=2**31)
        self.path = path
        if path.exists():
            for e in json.loads(path.read_text(encoding="utf-8"))["entries"]:
                self.put(e["model"], e["text"], e["embedding"])
        self.loaded = len(self)

    def save(self) -> str:
        """파일에 쓰고 내용 sha256을 돌려준다(provenance용)."""
        entries = [{"model": m, "text": t, "embedding": v} for (m, t), v in self._entries.items()]
        data = json.dumps({"entries": entries}, ensure_ascii=False).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()


def install_query_embedding_cache(cache: BoundedEmbeddingCache) -> BoundedEmbeddingCache:
    """`vector_search`가 쓰는 프로세스 전역 캐시를 `cache`로 바꾸고 이전 것을 돌려준다(복원용)."""
    global query_embedding_cache
    previous, query_embedding_cache = query_embedding_cache, cache
    return previous
