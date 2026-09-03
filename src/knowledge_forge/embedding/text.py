"""Builds the text an embedding is computed over, from a KnowledgeObject.

Deliberately does NOT reuse similarity.py's `.lower()` normalization -
that lowercasing is specific to difflib.SequenceMatcher's character-level
diffing, where case differences are just noise the ratio would otherwise
be sensitive to. An embedding model, by contrast, is trained on natural
text and treats case as a real signal (e.g. "Acme" as a proper noun vs.
"acme" as a common word) - lowercasing here would throw that signal away
for no benefit, so this keeps the original casing and only collapses
whitespace.
"""

from knowledge_forge.models.knowledge_object import KnowledgeObject


def build_embedding_text(ko: KnowledgeObject) -> str:
    """Concatenates `summary`, the stringified values of `content`, and
    `topics` into one whitespace-normalized string - the text actually sent
    to the embedding provider for this KnowledgeObject."""
    parts: list[str] = []
    if ko.summary:
        parts.append(ko.summary)
    for value in ko.content.values():
        if value is None:
            continue
        text = str(value)
        if text:
            parts.append(text)
    parts.extend(topic for topic in ko.topics if topic)
    return " ".join(" ".join(parts).split())
