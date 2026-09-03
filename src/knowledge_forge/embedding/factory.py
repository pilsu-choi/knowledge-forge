"""Picks the EmbeddingProvider implementation based on KF_EMBEDDING_PROVIDER -
mirrors extraction/factory.py's get_extractor(), the one switch point
pipeline.py/cli.py should use instead of hardcoding OpenAIEmbeddingProvider,
so a future provider swap doesn't ripple through the rest of the pipeline.
Only "openai" is implemented so far (see config.py's `embedding_provider`
Literal), but this keeps the switch point in place for when a second one is.
"""

from knowledge_forge.config import settings
from knowledge_forge.embedding.base import EmbeddingProvider
from knowledge_forge.embedding.openai_provider import OpenAIEmbeddingProvider


def get_embedding_provider(model: str | None = None) -> EmbeddingProvider:
    if settings.embedding_provider == "openai":
        return OpenAIEmbeddingProvider(model=model or settings.embedding_model)
    raise ValueError(f"unknown embedding_provider: {settings.embedding_provider}")
