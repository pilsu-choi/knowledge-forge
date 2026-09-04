"""Picks the EmbeddingProvider implementation based on KF_EMBEDDING_PROVIDER -
mirrors extraction/factory.py's get_extractor(), the one switch point
pipeline.py/cli.py should use instead of hardcoding OpenAIEmbeddingProvider,
so a provider swap doesn't ripple through the rest of the pipeline.
"openai" (the default, and the OpenAI-compatible endpoints reachable through
KF_EMBEDDING_BASE_URL) and "openrouter" are implemented - see config.py's
`embedding_provider` Literal.
"""

from knowledge_forge.config import settings
from knowledge_forge.embedding.base import EmbeddingProvider
from knowledge_forge.embedding.openai_provider import OpenAIEmbeddingProvider
from knowledge_forge.embedding.openrouter_provider import OpenRouterEmbeddingProvider


def get_embedding_provider(model: str | None = None) -> EmbeddingProvider:
    if settings.embedding_provider == "openai":
        return OpenAIEmbeddingProvider(model=model or settings.embedding_model)
    if settings.embedding_provider == "openrouter":
        # Deliberately reuses `embedding_model` rather than adding an
        # `openrouter_embedding_model` twin of `openrouter_extraction_model`:
        # unlike extraction (where one deployment routinely runs Anthropic
        # models directly AND has an OpenRouter fallback configured), the
        # embedding model has to stay fixed for a given corpus - vectors from
        # two different models are not comparable, and schema.sql holds one
        # vector(N) width. One key for "the embedding model in use" is
        # therefore the honest shape. Note the value must be an OpenRouter
        # slug here (e.g. "openai/text-embedding-3-small"), not a bare
        # OpenAI model name.
        return OpenRouterEmbeddingProvider(model=model or settings.embedding_model)
    raise ValueError(f"unknown embedding_provider: {settings.embedding_provider}")
