"""외부 리랭커 훅 - 가짜 서버(httpx.MockTransport)로 `/v1/rerank` 형식을 검증한다(`retrieve`의 rerank 단계 테스트는 test_retrieve.py). 실제 모델은 돌리지 않는다."""

from __future__ import annotations

import json

import httpx
import pytest

from knowledge_forge.config import settings
from knowledge_forge.search.remote_rerank import rerank_scores


async def test_rerank_scores_speaks_the_cohere_jina_rerank_format(monkeypatch):
    monkeypatch.setattr(settings, "reranker_base_url", "http://rerank.test/")
    monkeypatch.setattr(settings, "reranker_model", "bge-reranker-v2-m3")
    monkeypatch.setattr(settings, "reranker_api_key", "secret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        # 일부러 순서를 뒤집고 하나는 빠뜨린다.
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scores = await rerank_scores("질의", ["a", "b", "c"], client=client)

    assert seen["url"] == "http://rerank.test/v1/rerank" and seen["auth"] == "Bearer secret"
    assert seen["body"] == {"query": "질의", "documents": ["a", "b", "c"], "top_n": 3, "model": "bge-reranker-v2-m3"}
    assert scores[:2] == [0.1, 0.9] and scores[2] == float("-inf")


async def test_rerank_scores_requires_an_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "reranker_base_url", None)
    with pytest.raises(RuntimeError):
        await rerank_scores("q", ["a"])


async def test_rerank_scores_speaks_tei_native_format(monkeypatch):
    monkeypatch.setattr(settings, "reranker_base_url", "http://tei.test")
    monkeypatch.setattr(settings, "reranker_api", "tei")
    monkeypatch.setattr(settings, "reranker_api_key", "k")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, json=[{"index": 1, "score": 0.8}, {"index": 0, "score": 0.2}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scores = await rerank_scores("q", ["a", "b"], client=client)

    assert seen["url"] == "http://tei.test/rerank" and seen["auth"] == "Bearer k"
    assert seen["body"] == {"query": "q", "texts": ["a", "b"], "truncate": True}
    assert scores == [0.2, 0.8]
