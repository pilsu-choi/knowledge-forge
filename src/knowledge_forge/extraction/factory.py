"""Picks the Extractor implementation based on KF_LLM_PROVIDER - the one
switch point pipeline.py/cli.py should use instead of hardcoding
ClaudeExtractor, so a future provider swap doesn't ripple through the
rest of the pipeline (both implementations satisfy the same Extractor ABC).
"""

from knowledge_forge.config import settings
from knowledge_forge.extraction.base import Extractor
from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.openrouter_extractor import OpenRouterExtractor


def get_extractor(model: str | None = None) -> Extractor:
    if settings.llm_provider == "openrouter":
        return OpenRouterExtractor(model=model or settings.openrouter_extraction_model)
    return ClaudeExtractor(model=model or settings.extraction_model)
