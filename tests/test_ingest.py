"""Unit test for run_local_ingest (src/knowledge_forge/ingest.py), the
worker's pipeline-invocation helper - store=None here since only the
returned summary counts matter, not persistence (that's covered by
tests/storage/test_job_store.py and the worker's own live-DB test).
"""

from types import SimpleNamespace

from knowledge_forge.extraction.claude_extractor import ClaudeExtractor
from knowledge_forge.extraction.schemas import ExtractionResult
from knowledge_forge.ingest import run_local_ingest


class _FakeMessages:
    def __init__(self):
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            parsed_output=ExtractionResult(
                summary="stub summary",
                facts=[],
                entities=[],
                relations=[],
                topics=[],
                document_type="text",
            )
        )


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


async def test_run_local_ingest_returns_object_and_relation_counts(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("# A\n\nHello world.")
    (tmp_path / "b.txt").write_text("Just some plain text.")

    client = _FakeClient()
    monkeypatch.setattr(
        "knowledge_forge.ingest.get_extractor", lambda model=None: ClaudeExtractor(client=client)
    )

    result = await run_local_ingest(tmp_path, max_tokens=4000, model=None, store=None)

    assert result["object_count"] >= 2  # at least one KO per file
    assert "relation_count" in result
    assert client.messages.calls >= 2
