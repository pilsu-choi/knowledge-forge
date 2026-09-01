from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.vision.extractor import VisionExtractor

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class _FakeMessages:
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class _FakeClient:
    def __init__(self, side_effects):
        self.messages = _FakeMessages(side_effects)


def _text_response(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


async def test_extract_table_markdown_returns_text_and_sends_image_block():
    client = _FakeClient([_text_response("| a | b |\n|---|---|\n| 1 | 2 |")])
    extractor = VisionExtractor(client=client)

    result = await extractor.extract_table_markdown(b"fake-png-bytes", media_type="image/png")

    assert result == "| a | b |\n|---|---|\n| 1 | 2 |"
    content = client.messages.last_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1]["type"] == "text"


async def test_describe_figure_uses_figure_prompt():
    client = _FakeClient([_text_response("A bar chart showing revenue growth.")])
    extractor = VisionExtractor(client=client)

    result = await extractor.describe_figure(b"fake-png-bytes")

    assert "revenue" in result
    prompt_text = client.messages.last_kwargs["messages"][0]["content"][1]["text"]
    assert "figure" in prompt_text.lower() or "chart" in prompt_text.lower()


async def test_retries_once_on_rate_limit_then_succeeds():
    response = httpx2.Response(429, request=_REQUEST, json={"error": {"type": "rate_limit_error", "message": "slow"}})
    err = anthropic.RateLimitError("slow", response=response, body=None)
    client = _FakeClient([err, _text_response("ok")])

    result = await VisionExtractor(client=client).describe_figure(b"x")

    assert result == "ok"
    assert client.messages.calls == 2


async def test_bad_request_raises_knowledge_extraction_error():
    response = httpx2.Response(400, request=_REQUEST, json={"error": {"type": "invalid_request_error", "message": "bad"}})
    err = anthropic.BadRequestError("bad", response=response, body=None)
    client = _FakeClient([err])

    with pytest.raises(KnowledgeExtractionError):
        await VisionExtractor(client=client).extract_table_markdown(b"x")
