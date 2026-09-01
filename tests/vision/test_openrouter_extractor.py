from types import SimpleNamespace

import httpx2
import openai
import pytest

from knowledge_forge.exceptions import KnowledgeExtractionError
from knowledge_forge.vision.openrouter_extractor import OpenRouterVisionExtractor

_REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def _rate_limit_error() -> openai.RateLimitError:
    response = httpx2.Response(429, request=_REQUEST, json={"error": {"message": "slow"}})
    return openai.RateLimitError("slow", response=response, body=None)


def _bad_request_error() -> openai.BadRequestError:
    response = httpx2.Response(400, request=_REQUEST, json={"error": {"message": "bad"}})
    return openai.BadRequestError("bad", response=response, body=None)


def _chat_response(content: str):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


class _FakeCompletions:
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


class _FakeChat:
    def __init__(self, side_effects):
        self.completions = _FakeCompletions(side_effects)


class _FakeClient:
    def __init__(self, side_effects):
        self.chat = _FakeChat(side_effects)


async def test_extract_table_markdown_returns_text_and_sends_image_url_block():
    client = _FakeClient([_chat_response("| a | b |\n|---|---|\n| 1 | 2 |")])
    extractor = OpenRouterVisionExtractor(client=client)

    result = await extractor.extract_table_markdown(b"fake-png-bytes", media_type="image/png")

    assert result == "| a | b |\n|---|---|\n| 1 | 2 |"
    content = client.chat.completions.last_kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["type"] == "text"


async def test_describe_figure_uses_figure_prompt():
    client = _FakeClient([_chat_response("A bar chart showing revenue growth.")])
    extractor = OpenRouterVisionExtractor(client=client)

    result = await extractor.describe_figure(b"fake-png-bytes")

    assert "revenue" in result
    prompt_text = client.chat.completions.last_kwargs["messages"][0]["content"][1]["text"]
    assert "figure" in prompt_text.lower() or "chart" in prompt_text.lower()


async def test_retries_once_on_rate_limit_then_succeeds():
    client = _FakeClient([_rate_limit_error(), _chat_response("ok")])

    result = await OpenRouterVisionExtractor(client=client).describe_figure(b"x")

    assert result == "ok"
    assert client.chat.completions.calls == 2


async def test_bad_request_raises_knowledge_extraction_error():
    client = _FakeClient([_bad_request_error()])

    with pytest.raises(KnowledgeExtractionError):
        await OpenRouterVisionExtractor(client=client).extract_table_markdown(b"x")
