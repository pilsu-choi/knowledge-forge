"""외부 리랭커(크로스 인코더) 호출 - `retrieve.py::_Run.rerank`가 융합 상위 N건을 재정렬할 때 쓴다.

Cohere/Jina 호환 `POST {KF_RERANKER_BASE_URL}/v1/rerank`(vLLM·Infinity·Jina·Cohere가 같은 형식)만 지원한다:
요청 `{model, query, documents, top_n}`, 응답 `{results: [{index, relevance_score}]}`. 모델 실행은 이 프로세스 밖이다.
"""

from __future__ import annotations

from typing import Any

import httpx

from knowledge_forge.config import settings


async def rerank_scores(query: str, documents: list[str], *, client: httpx.AsyncClient | None = None) -> list[float]:
    """`documents`와 같은 순서의 관련도 점수. 응답에 빠진 문서는 `-inf`(맨 뒤)다.
    엔드포인트 미설정/HTTP 오류/형식 오류는 예외로 올린다 - 호출자(`_Run.rerank`)가 융합 순서로 되돌아간다."""
    if not settings.reranker_base_url:
        raise RuntimeError("KF_RERANKER_BASE_URL is not set")
    headers = {"Authorization": f"Bearer {settings.reranker_api_key}"} if settings.reranker_api_key else {}
    payload: dict[str, Any] = {"query": query, "documents": documents, "top_n": len(documents)}
    if settings.reranker_model:
        payload["model"] = settings.reranker_model
    url = settings.reranker_base_url.rstrip("/") + "/v1/rerank"
    owned = client is None
    client = client or httpx.AsyncClient(timeout=settings.reranker_timeout)
    try:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        results = response.json()["results"]
    finally:
        if owned:
            await client.aclose()
    scores = [float("-inf")] * len(documents)
    for item in results:
        scores[int(item["index"])] = float(item["relevance_score"])
    return scores
