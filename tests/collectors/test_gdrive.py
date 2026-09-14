from datetime import datetime, timezone

from knowledge_forge.collectors.base import Cursor
from knowledge_forge.collectors.saas.gdrive import GoogleDriveCollector


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeFilesResource:
    def __init__(self, list_pages, media_by_id=None, export_by_id=None):
        self._list_pages = list(list_pages)
        self._media_by_id = media_by_id or {}
        self._export_by_id = export_by_id or {}
        self.list_calls = []
        self.get_media_calls = []
        self.export_media_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _FakeExecutable(self._list_pages.pop(0))

    def get_media(self, **kwargs):
        self.get_media_calls.append(kwargs)
        return _FakeExecutable(self._media_by_id[kwargs["fileId"]])

    def export_media(self, **kwargs):
        self.export_media_calls.append(kwargs)
        return _FakeExecutable(self._export_by_id[(kwargs["fileId"], kwargs["mimeType"])])


class _FakeChangesResource:
    def __init__(self, list_pages, start_page_token="token-0"):
        self._list_pages = list(list_pages)
        self._start_page_token = start_page_token
        self.list_calls = []

    def getStartPageToken(self, **kwargs):
        return _FakeExecutable({"startPageToken": self._start_page_token})

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _FakeExecutable(self._list_pages.pop(0))


class _FakeDriveClient:
    def __init__(self, files_resource, changes_resource):
        self._files = files_resource
        self._changes = changes_resource

    def files(self):
        return self._files

    def changes(self):
        return self._changes


def test_initial_sync_paginates_downloads_and_exports_native_files():
    list_pages = [
        {
            "files": [
                {"id": "f1", "name": "notes.txt", "mimeType": "text/plain", "modifiedTime": "2024-01-15T10:30:00.000Z"},
            ],
            "nextPageToken": "p2",
        },
        {
            "files": [
                {
                    "id": "f2",
                    "name": "doc",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2024-01-16T00:00:00Z",
                    "webViewLink": "https://drive/f2",
                },
            ],
            "nextPageToken": None,
        },
    ]
    files = _FakeFilesResource(
        list_pages,
        media_by_id={"f1": b"plain file bytes"},
        export_by_id={("f2", "text/plain"): b"exported doc text"},
    )
    changes = _FakeChangesResource([], start_page_token="start-1")
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client, folder_id="folder123")
    items = list(collector.initial_sync())

    assert len(items) == 2
    assert items[0].content == b"plain file bytes"
    assert items[0].uri == "gdrive://f1"
    assert items[0].modified_at == datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)

    assert items[1].content == b"exported doc text"
    assert items[1].uri == "https://drive/f2"
    assert files.export_media_calls[0]["mimeType"] == "text/plain"
    assert files.export_media_calls[0]["fileId"] == "f2"

    assert "'folder123' in parents" in files.list_calls[0]["q"]
    assert files.list_calls[1]["pageToken"] == "p2"

    assert collector.get_cursor().data["start_page_token"] == "start-1"


def test_initial_sync_recurses_into_subfolders_without_downloading_them():
    # Drive's `'{id}' in parents` query only lists direct children, so a
    # subfolder found under the scoped root must be walked separately - and
    # never handed to export/get_media, which 403s on folder ids.
    list_pages_by_query = {
        "'root' in parents": [{
            "files": [
                {"id": "sub", "name": "Sub", "mimeType": "application/vnd.google-apps.folder", "modifiedTime": "2024-01-01T00:00:00Z"},
                {"id": "f1", "name": "top.txt", "mimeType": "text/plain", "modifiedTime": "2024-01-01T00:00:00Z"},
            ],
            "nextPageToken": None,
        }],
        "'sub' in parents": [{
            "files": [
                {"id": "f2", "name": "nested.txt", "mimeType": "text/plain", "modifiedTime": "2024-01-02T00:00:00Z"},
            ],
            "nextPageToken": None,
        }],
    }

    class _FakeFilesResourceByQuery(_FakeFilesResource):
        def __init__(self, media_by_id):
            super().__init__([], media_by_id=media_by_id)

        def list(self, **kwargs):
            self.list_calls.append(kwargs)
            return _FakeExecutable(list_pages_by_query[kwargs["q"]].pop(0))

    files = _FakeFilesResourceByQuery(media_by_id={"f1": b"top", "f2": b"nested"})
    changes = _FakeChangesResource([], start_page_token="start-1")
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client, folder_id="root")
    items = list(collector.initial_sync())

    assert {item.uri for item in items} == {"gdrive://f1", "gdrive://f2"}
    assert not files.export_media_calls


def test_incremental_sync_yields_changed_and_removed_files():
    change_pages = [
        {
            "changes": [
                {
                    "fileId": "f9",
                    "removed": False,
                    "file": {"id": "f9", "name": "new.txt", "mimeType": "text/plain", "modifiedTime": "2024-02-01T00:00:00Z"},
                },
                {"fileId": "f1", "removed": True},
            ],
            "nextPageToken": None,
            "newStartPageToken": "start-2",
        }
    ]
    files = _FakeFilesResource([], media_by_id={"f9": b"new content"})
    changes = _FakeChangesResource(change_pages)
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client)
    cursor = Cursor(data={"start_page_token": "start-1"})
    items = list(collector.incremental_sync(cursor))

    assert len(items) == 2
    normal = next(i for i in items if not i.deleted)
    removed = next(i for i in items if i.deleted)
    assert normal.uri == "gdrive://f9"
    assert normal.content == b"new content"
    assert removed.uri == "gdrive://f1"
    assert removed.content == b""

    assert changes.list_calls[0]["pageToken"] == "start-1"
    assert collector.get_cursor().data["start_page_token"] == "start-2"


def test_incremental_sync_keeps_prior_token_when_no_new_token_given():
    change_pages = [{"changes": [], "nextPageToken": None}]  # no newStartPageToken in response
    files = _FakeFilesResource([])
    changes = _FakeChangesResource(change_pages)
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client)
    cursor = Cursor(data={"start_page_token": "start-1"})
    list(collector.incremental_sync(cursor))

    assert collector.get_cursor().data["start_page_token"] == "start-1"


# --- 다운로드 무결성 검사 (item 6) -------------------------------------------
# 실제 사고: 확장자/mimeType은 xlsx인데 실제로 받은 바이트는 Google Sheets의
# HTML 페이지였다(<!DOCTYPE html>, 292KB) - content_hash는 그 바이트를 그대로
# 축복해서 정상 수집처럼 보였다(wiki/reingest-replay.md).


def test_initial_sync_skips_a_file_whose_bytes_dont_match_its_declared_extension(caplog):
    list_pages = [
        {
            "files": [
                {
                    "id": "bad1",
                    "name": "report.xlsx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "modifiedTime": "2024-01-15T10:30:00.000Z",
                    "webViewLink": "https://drive/bad1",
                },
                {"id": "ok1", "name": "notes.txt", "mimeType": "text/plain", "modifiedTime": "2024-01-15T10:30:00.000Z"},
            ],
            "nextPageToken": None,
        }
    ]
    files = _FakeFilesResource(
        list_pages,
        media_by_id={"bad1": b"<!DOCTYPE html><html><body>sign in</body></html>", "ok1": b"plain text"},
    )
    changes = _FakeChangesResource([], start_page_token="start-1")
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client)
    with caplog.at_level("ERROR"):
        items = list(collector.initial_sync())

    assert [i.uri for i in items] == ["gdrive://ok1"]  # the HTML-disguised xlsx is skipped, not yielded
    assert any("bad1" in r.message or "https://drive/bad1" in r.message for r in caplog.records)


def test_incremental_sync_skips_a_file_whose_bytes_dont_match_its_declared_extension():
    change_pages = [
        {
            "changes": [
                {
                    "fileId": "bad2",
                    "removed": False,
                    "file": {
                        "id": "bad2",
                        "name": "contract.pdf",
                        "mimeType": "application/pdf",
                        "modifiedTime": "2024-02-01T00:00:00Z",
                    },
                },
                {
                    "fileId": "ok2",
                    "removed": False,
                    "file": {"id": "ok2", "name": "real.pdf", "mimeType": "application/pdf", "modifiedTime": "2024-02-01T00:00:00Z"},
                },
            ],
            "nextPageToken": None,
            "newStartPageToken": "start-2",
        }
    ]
    files = _FakeFilesResource(
        [], media_by_id={"bad2": b"<html>redirected to login</html>", "ok2": b"%PDF-1.4 real pdf bytes"}
    )
    changes = _FakeChangesResource(change_pages)
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client)
    items = list(collector.incremental_sync(Cursor(data={"start_page_token": "start-1"})))

    assert [i.uri for i in items] == ["gdrive://ok2"]


def test_zip_based_ooxml_and_pdf_magic_bytes_are_accepted():
    list_pages = [
        {
            "files": [
                {"id": "x1", "name": "a.xlsx", "mimeType": "...", "modifiedTime": "2024-01-01T00:00:00Z"},
                {"id": "x2", "name": "a.docx", "mimeType": "...", "modifiedTime": "2024-01-01T00:00:00Z"},
                {"id": "x3", "name": "a.pptx", "mimeType": "...", "modifiedTime": "2024-01-01T00:00:00Z"},
                {"id": "x4", "name": "a.hwpx", "mimeType": "...", "modifiedTime": "2024-01-01T00:00:00Z"},
                {"id": "x5", "name": "a.pdf", "mimeType": "...", "modifiedTime": "2024-01-01T00:00:00Z"},
            ],
            "nextPageToken": None,
        }
    ]
    zip_bytes = b"PK\x03\x04rest of a real zip"
    files = _FakeFilesResource(
        list_pages,
        media_by_id={
            "x1": zip_bytes,
            "x2": zip_bytes,
            "x3": zip_bytes,
            "x4": zip_bytes,
            "x5": b"%PDF-1.7 rest of a real pdf",
        },
    )
    changes = _FakeChangesResource([], start_page_token="start-1")
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client)
    items = list(collector.initial_sync())

    assert {i.uri for i in items} == {"gdrive://x1", "gdrive://x2", "gdrive://x3", "gdrive://x4", "gdrive://x5"}


def test_extensions_outside_the_checked_set_are_not_validated():
    """확장자가 검사 대상(xlsx/docx/pptx/hwpx/pdf)이 아니면 - 예: 텍스트/csv -
    바이트 내용과 무관하게 통과한다 (Workspace 파일 export 결과처럼 확장자와
    실제 포맷이 무관한 경우를 오탐하지 않기 위함)."""
    list_pages = [
        {
            "files": [
                {"id": "c1", "name": "data.csv", "mimeType": "text/csv", "modifiedTime": "2024-01-01T00:00:00Z"},
            ],
            "nextPageToken": None,
        }
    ]
    files = _FakeFilesResource(list_pages, media_by_id={"c1": b"<!DOCTYPE html>not really csv but not checked"})
    changes = _FakeChangesResource([], start_page_token="start-1")
    client = _FakeDriveClient(files, changes)

    collector = GoogleDriveCollector(client=client)
    items = list(collector.initial_sync())

    assert [i.uri for i in items] == ["gdrive://c1"]
