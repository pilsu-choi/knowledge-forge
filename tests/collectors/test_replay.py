from datetime import datetime, timezone

from knowledge_forge.chunking import AdaptiveChunker
from knowledge_forge.collectors.replay import (
    RawBlobReplayCollector,
    ReplayRow,
    filter_rows_by_extension,
)
from knowledge_forge.ids import content_hash
from knowledge_forge.models.source import SourceType
from knowledge_forge.parsers.registry import default_registry
from knowledge_forge.pipeline import run_pipeline

MODIFIED_AT = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _blob(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _row(tmp_path, *, uri: str, name: str, data: bytes = b"hello", project_id: str = "default") -> ReplayRow:
    return ReplayRow(
        uri=uri,
        raw_path=_blob(tmp_path, content_hash(data), data),
        modified_at=MODIFIED_AT,
        metadata={"name": name},
        project_id=project_id,
    )


def test_yields_raw_items_from_disk_blobs(tmp_path):
    row = _row(tmp_path, uri="https://drive.google.com/file/d/abc/view", name="notes.md", data=b"# hi")
    collector = RawBlobReplayCollector([row], SourceType.GOOGLE_DRIVE)

    items = list(collector.sync())

    assert len(items) == 1
    assert items[0].uri == row.uri
    assert items[0].content == b"# hi"
    assert items[0].content_hash == content_hash(b"# hi")
    assert items[0].modified_at == MODIFIED_AT
    # metadata["name"] is what the parser registry falls back to for the
    # extension when the uri has none - it must survive the round trip.
    assert items[0].metadata["name"] == "notes.md"


def test_adopts_the_source_type_of_the_replayed_rows():
    """`_make_source` derives source_id from (source_type, uri), so a replay
    that reported some other type would write duplicate `sources` rows
    instead of filling in the existing ones."""
    collector = RawBlobReplayCollector([], SourceType.GOOGLE_DRIVE)

    assert collector.source_type is SourceType.GOOGLE_DRIVE


def test_per_row_project_id_is_carried_onto_the_raw_item(tmp_path):
    row = _row(tmp_path, uri="https://drive.google.com/file/d/a/view", name="a.md", project_id="gdrive-live-check")

    (item,) = list(RawBlobReplayCollector([row], SourceType.GOOGLE_DRIVE).sync())

    assert item.project_id == "gdrive-live-check"


def test_skips_rows_whose_blob_is_gone(tmp_path):
    present = _row(tmp_path, uri="https://drive.google.com/file/d/a/view", name="a.md", data=b"a")
    missing = ReplayRow(
        uri="https://drive.google.com/file/d/b/view",
        raw_path=str(tmp_path / "does-not-exist"),
        modified_at=MODIFIED_AT,
        metadata={"name": "b.md"},
    )

    items = list(RawBlobReplayCollector([missing, present], SourceType.GOOGLE_DRIVE).sync())

    assert [item.uri for item in items] == [present.uri]


def test_get_cursor_is_empty_so_a_replay_never_moves_the_real_sync_position():
    assert RawBlobReplayCollector([], SourceType.GOOGLE_DRIVE).get_cursor().data == {}


def test_extension_comes_from_the_filename_when_the_uri_has_none(tmp_path):
    row = _row(tmp_path, uri="https://drive.google.com/file/d/abc/view?usp=drivesdk", name="보고서.xlsx")

    assert row.extension == ".xlsx"


def test_filter_rows_by_extension(tmp_path):
    md = _row(tmp_path, uri="https://drive.google.com/file/d/a/view", name="a.md", data=b"a")
    png = _row(tmp_path, uri="https://drive.google.com/file/d/b/view", name="b.png", data=b"b")

    assert filter_rows_by_extension([md, png], {".md"}) == [md]


async def test_replayed_rows_run_through_the_pipeline_into_documents(tmp_path):
    """The whole point: a Drive uri with no extension, which the registry
    used to reject outright, parses when replayed through today's code."""
    row = _row(
        tmp_path,
        uri="https://drive.google.com/file/d/abc/view?usp=drivesdk",
        name="한글제목.md",
        data="# 제목\n\n본문입니다.\n".encode(),
    )
    collector = RawBlobReplayCollector([row], SourceType.GOOGLE_DRIVE)

    outcomes = [
        outcome
        async for outcome in run_pipeline(
            collector, default_registry(), AdaptiveChunker(max_tokens_per_chunk=100), extractor=None
        )
    ]

    assert len(outcomes) == 1
