"""Picks the vision-extraction implementation based on KF_LLM_PROVIDER -
mirrors extraction.factory.get_extractor(). All five parsers that need
VLM (pdf/pptx/docx/hwpx/image) build their lazily-constructed instance
through this function instead of hardcoding VisionExtractor, so provider
selection actually reaches them.

`claude_agent_sdk` now has a real implementation
(`ClaudeAgentSDKVisionExtractor`, see that module's docstring for how it
carries image content through the CLI's streaming-input protocol) - before
this fix, that provider value silently fell through to `VisionExtractor`
(the direct Anthropic SDK path), which needs `ANTHROPIC_API_KEY`. On a
machine running `claude_agent_sdk` with no such key, every VLM call failed
authentication and every table/figure degraded to its rule-based/empty
fallback with only a per-call warning log - see
wiki/vision-extraction.md's "claude_agent_sdk 아래 VLM이 조용히 실패하던
문제" for the 869-call incident this was found from.
"""

from knowledge_forge.config import settings
from knowledge_forge.vision.claude_agent_sdk_extractor import ClaudeAgentSDKVisionExtractor
from knowledge_forge.vision.extractor import VisionExtractor
from knowledge_forge.vision.openrouter_extractor import OpenRouterVisionExtractor

# The concrete implementations get_vision_extractor() can hand back - none
# are related by inheritance (each wraps a different SDK), so callers that
# lazily cache the result (every parser's self._vision) need this union
# rather than plain VisionExtractor.
VisionExtractorLike = VisionExtractor | OpenRouterVisionExtractor | ClaudeAgentSDKVisionExtractor


def get_vision_extractor(model: str | None = None) -> VisionExtractorLike:
    if settings.llm_provider == "openrouter":
        return OpenRouterVisionExtractor(model=model or settings.openrouter_vision_model)
    if settings.llm_provider == "claude_agent_sdk":
        return ClaudeAgentSDKVisionExtractor(model=model or settings.claude_agent_sdk_vision_model)
    return VisionExtractor(model=model) if model else VisionExtractor()
