from datetime import datetime, timezone

from knowledge_forge.embedding.text import build_embedding_text
from knowledge_forge.models.knowledge_object import (
    DocumentRef,
    KnowledgeObject,
    KnowledgeObjectType,
)
from knowledge_forge.models.source import SourceRef, SourceType


def _make_ko(summary: str, content: dict, topics: list[str]) -> KnowledgeObject:
    return KnowledgeObject(
        knowledge_object_id="ko1",
        type=KnowledgeObjectType.FACT,
        source=SourceRef(source_id="s1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id="d1", title="T"),
        evidence=[],
        summary=summary,
        content=content,
        topics=topics,
        document_type="report",
        extracted_at=datetime.now(timezone.utc),
        model_used="claude-opus-5",
        pipeline_version="0.1.0",
        source_hash="h1",
    )


def test_combines_summary_content_values_and_topics():
    ko = _make_ko(
        summary="Acme Corp signed the contract.",
        content={"statement": "The Board approved the merger."},
        topics=["contracts", "mergers"],
    )

    text = build_embedding_text(ko)

    assert text == "Acme Corp signed the contract. The Board approved the merger. contracts mergers"


def test_normalizes_whitespace_and_newlines():
    ko = _make_ko(
        summary="Line one\nLine  two",
        content={"statement": "  extra   spaces  here "},
        topics=["  topic\twith\ttabs  "],
    )

    text = build_embedding_text(ko)

    assert text == "Line one Line two extra spaces here topic with tabs"
    assert "\n" not in text
    assert "\t" not in text


def test_preserves_case():
    ko = _make_ko(summary="Acme CORP Signed", content={}, topics=["Contracts"])

    text = build_embedding_text(ko)

    assert text == "Acme CORP Signed Contracts"
    assert text != text.lower()


def test_handles_non_string_content_values_without_raising():
    ko = _make_ko(
        summary="Summary here",
        content={"nested": {"a": 1, "b": [1, 2, 3]}, "count": 42, "flag": True},
        topics=[],
    )

    text = build_embedding_text(ko)

    assert "Summary here" in text
    assert "42" in text
    assert "True" in text
    assert "{'a': 1, 'b': [1, 2, 3]}" in text


def test_skips_empty_values():
    ko = _make_ko(
        summary="",
        content={"empty": "", "none": None, "statement": "kept"},
        topics=["", "kept-topic"],
    )

    text = build_embedding_text(ko)

    assert text == "kept kept-topic"
