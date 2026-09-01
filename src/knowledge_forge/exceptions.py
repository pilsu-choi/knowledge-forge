class KnowledgeForgeError(Exception):
    """Base exception for all knowledge_forge errors."""


class UnsupportedFormatError(KnowledgeForgeError):
    """Raised when no parser is registered for a given file type."""


class ParsingError(KnowledgeForgeError):
    """Raised when a parser fails to convert raw content into a Document."""


class KnowledgeExtractionError(KnowledgeForgeError):
    """Raised when the LLM extraction step fails for a chunk (non-retryable)."""

    def __init__(self, message: str, chunk_id: str | None = None):
        super().__init__(message)
        self.chunk_id = chunk_id
