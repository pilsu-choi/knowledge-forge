"""외부 리랭커(크로스 인코더) 호출 - `retrieve.py::_Run.rerank`가 융합 상위 N건을 재정렬할 때 쓴다.

`KF_RERANKER_API`로 형식을 고른다. `cohere`(기본): `POST {base}/v1/rerank`, 요청 `{model, query, documents, top_n}`,
응답 `{results: [{index, relevance_score}]}`(vLLM·Infinity·Jina·Cohere). `tei`: HuggingFace TEI의 `POST {base}/rerank`,
요청 `{query, texts, truncate}`, 응답 `[{index, score}]`. 모델 실행은 이 프로세스 밖이다.
"""

from __future__ import annotations

import httpx

from knowledge_forge.config import settings


async def rerank_scores(query: str, documents: list[str], *, client: httpx.AsyncClient | None = None) -> list[float]:
    """`documents`와 같은 순서의 관련도 점수. 응답에 빠진 문서는 `-inf`(맨 뒤)다.
    엔드포인트 미설정/HTTP 오류/형식 오류는 예외로 올린다 - 호출자(`_Run.rerank`)가 융합 순서로 되돌아간다."""
    if not settings.reranker_base_url:
        raise RuntimeError("KF_RERANKER_BASE_URL is not set")
    headers = {"Authorization": f"Bearer {settings.reranker_api_key}"} if settings.reranker_api_key else {}
    tei = settings.reranker_api == "tei"
    base = settings.reranker_base_url.rstrip("/")
    if tei:  # 긴 텍스트는 서버에서 자르게 한다(truncate)
        url, payload = base + "/rerank", {"query": query, "texts": documents, "truncate": True}
    else:
        url, payload = base + "/v1/rerank", {"query": query, "documents": documents, "top_n": len(documents)}
        if settings.reranker_model:
            payload["model"] = settings.reranker_model
    owned = client is None
    client = client or httpx.AsyncClient(timeout=settings.reranker_timeout)
    try:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
        results = body if tei else body["results"]
    finally:
        if owned:
            await client.aclose()
    scores = [float("-inf")] * len(documents)
    for item in results:
        scores[int(item["index"])] = float(item["score" if tei else "relevance_score"])
    return scores
