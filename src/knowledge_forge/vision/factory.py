"""Picks the vision-extraction implementation based on KF_LLM_PROVIDER -
mirrors extraction.factory.get_extractor(). All five parsers that need
VLM (pdf/pptx/docx/hwpx/image) build their lazily-constructed instance
through this function instead of hardcoding VisionExtractor, so provider
selection actually reaches them.
"""

from knowledge_forge.config import settings
from knowledge_forge.vision.extractor import VisionExtractor
from knowledge_forge.vision.openrouter_extractor import OpenRouterVisionExtractor


def get_vision_extractor(model: str | None = None) -> "VisionExtractor | OpenRouterVisionExtractor":
    if settings.llm_provider == "openrouter":
        return OpenRouterVisionExtractor(model=model or settings.openrouter_vision_model)
    return VisionExtractor(model=model) if model else VisionExtractor()
