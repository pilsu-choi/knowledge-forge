import json
from datetime import datetime, timezone

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider
from knowledge_forge.evaluation.judge import judge_search_log
from knowledge_forge.storage.eval_store import SearchLog


class _FakeProvider(ChatProvider):
    def __init__(self, text: str | None = None, exc: Exception | None = None):
        self._text = text
        self._exc = exc

    async def send(self, messages, tools, system) -> AssistantTurn:
        if self._exc is not None:
            raise self._exc
        return AssistantTurn(text=self._text, tool_calls=[], stop_reason="end_turn")


def _log(query="q", answer="a", evidence=None) -> SearchLog:
    return SearchLog(
        log_id="log-1",
        query=query,
        answer=answer,
        evidence=evidence or [],
        tool_calls=[],
        latency_ms=100,
        created_at=datetime.now(timezone.utc),
    )


async def test_judge_scores_fully_supported_answer():
    response = json.dumps(
        {
            "claims": [
                {"text": "A 결정이 있었다", "verdict": "supported", "reason": "evidence에 있음", "citation_valid": True}
            ]
        }
    )
    provider = _FakeProvider(text=response)

    result = await judge_search_log(_log(), provider)

    assert result is not None
    assert result.groundedness_score == 1.0
    assert result.citation_precision == 1.0
    assert len(result.claims) == 1


async def test_judge_scores_partially_supported_answer():
    response = json.dumps(
        {
            "claims": [
                {"text": "claim 1", "verdict": "supported", "reason": "r", "citation_valid": None},
                {"text": "claim 2", "verdict": "unsupported", "reason": "r", "citation_valid": None},
            ]
        }
    )
    provider = _FakeProvider(text=response)

    result = await judge_search_log(_log(), provider)

    assert result.groundedness_score == 0.5
    assert result.citation_precision is None  # no claim carried a citation


async def test_judge_treats_no_checkable_claims_as_fully_grounded():
    provider = _FakeProvider(text=json.dumps({"claims": []}))

    result = await judge_search_log(_log(answer="근거를 찾지 못했습니다."), provider)

    assert result.groundedness_score == 1.0
    assert result.citation_precision is None
    assert result.claims == []


async def test_judge_returns_none_when_response_is_not_json():
    provider = _FakeProvider(text="this is not json")

    assert await judge_search_log(_log(), provider) is None


async def test_judge_returns_none_when_claims_field_is_missing():
    provider = _FakeProvider(text=json.dumps({"unexpected": "shape"}))

    assert await judge_search_log(_log(), provider) is None


async def test_judge_returns_none_when_provider_call_raises():
    provider = _FakeProvider(exc=RuntimeError("network down"))

    assert await judge_search_log(_log(), provider) is None


async def test_judge_drops_malformed_individual_claims_but_keeps_valid_ones():
    response = json.dumps(
        {
            "claims": [
                {"text": "good claim", "verdict": "supported", "reason": "r"},
                {"text": "bad verdict", "verdict": "maybe", "reason": "r"},
                {"verdict": "supported", "reason": "missing text"},
            ]
        }
    )
    provider = _FakeProvider(text=response)

    result = await judge_search_log(_log(), provider)

    assert len(result.claims) == 1
    assert result.claims[0]["text"] == "good claim"
