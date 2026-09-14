"""`retrieve(grain=...)` narrows every vector lookup to one embedding grain; None follows KF_EMBEDDING_SOURCE (never mixes grains)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from knowledge_forge.config import settings
from knowledge_forge.search import retrieve as retrieve_module
from knowledge_forge.search.retrieve import retrieve

_NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def _capture_vector(monkeypatch) -> list:
    kinds: list = []

    async def _fake(query, store, project_id=None, type=None, limit=10, embedding_model=None, since=None, until=None, target_kind=None, document_ids=None):
        if document_ids is not None:  # grain expansion lookup, not the vector arm
            return []
        kinds.append(target_kind)
        return [{"target_kind": "chunk", "chunk_id": "c1", "document_id": "d1", "knowledge_object_id": None, "score": 0.9}]

    monkeypatch.setattr(retrieve_module, "vector_search", _fake)
    return kinds


@pytest.mark.parametrize("grain", [None, "chunk", "knowledge_object"])
async def test_vector_arm_passes_grain_as_target_kind(monkeypatch, fakes, grain):
    kinds = _capture_vector(monkeypatch)

    await retrieve("배포 일정 조율 과정", store=fakes.FakeStore(), now=_NOW, grain=grain, strategies_override=["vector"])

    assert kinds == [grain or settings.embedding_source]


async def test_grain_none_follows_the_embedding_source_setting(monkeypatch, fakes):
    kinds = _capture_vector(monkeypatch)
    monkeypatch.setattr(settings, "embedding_source", "chunk")

    await retrieve("배포 일정 조율 과정", store=fakes.FakeStore(), now=_NOW, strategies_override=["vector"])

    assert kinds == ["chunk"]


async def test_grain_none_matches_omitting_the_argument(monkeypatch, fakes):
    _capture_vector(monkeypatch)

    a = await retrieve("배포 일정 조율 과정", store=fakes.FakeStore(), now=_NOW)
    b = await retrieve("배포 일정 조율 과정", store=fakes.FakeStore(), now=_NOW, grain=None)

    assert (a.hits, a.document_ids, a.strategies) == (b.hits, b.document_ids, b.strategies)


async def test_multihop_planner_receives_grain(monkeypatch, fakes):
    _capture_vector(monkeypatch)
    seen: dict = {}

    async def _fake_plan(query, provider, *, store=None, project_id=None, limit_per_subquery=10, target_kind=None):
        seen["target_kind"] = target_kind
        return {"subqueries": [], "evidence": []}

    monkeypatch.setattr(retrieve_module, "plan_and_search", _fake_plan)

    await retrieve("q", store=fakes.FakeStore(), provider=fakes.ScriptedProvider(), use_llm_constraints=False, now=_NOW, grain="chunk", strategies_override=["multihop"])

    assert seen == {"target_kind": "chunk"}


async def test_unknown_grain_is_rejected(fakes):
    with pytest.raises(ValueError):
        await retrieve("q", store=fakes.FakeStore(), now=_NOW, grain="document")
