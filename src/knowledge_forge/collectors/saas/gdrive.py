import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType

logger = logging.getLogger(__name__)

_LIST_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)"
_CHANGES_FIELDS = "nextPageToken, newStartPageToken, changes(fileId, removed, file(id, name, mimeType, modifiedTime, webViewLink))"

# Magic-byte check for `get_media()` downloads (raw bytes of a non-Workspace
# file) - `export_media()` output is intentionally a different format
# (text/plain or text/csv, see _EXPORT_MIME_TYPES) regardless of the
# original file's extension, so it is never checked here. Extension-keyed
# (not mimeType-keyed): Drive's own `mimeType` field isn't reliable for
# every one of these (hwpx in particular has no well-known registered mime
# type), and this mirrors how parsers/registry.py already dispatches by
# extension - "what the file claims to be" downstream is the extension.
_ZIP_MAGIC = b"PK\x03\x04"  # xlsx/docx/pptx/hwpx are all zip containers
_PDF_MAGIC = b"%PDF"
_EXPECTED_MAGIC_BY_EXTENSION: dict[str, bytes] = {
    ".xlsx": _ZIP_MAGIC,
    ".docx": _ZIP_MAGIC,
    ".pptx": _ZIP_MAGIC,
    ".hwpx": _ZIP_MAGIC,
    ".pdf": _PDF_MAGIC,
}


class DownloadIntegrityError(Exception):
    """A downloaded file's bytes don't start with the magic bytes its
    extension declares - e.g. an HTML error/redirect page (`<!DOCTYPE
    html...`) served back instead of the real xlsx/docx/pptx/hwpx/pdf bytes
    (see wiki/reingest-replay.md's "gdrive 다운로드 무결성 검사" for the
    incident this guards against - content_hash alone happily hashes
    whatever bytes arrive, corrupt or not)."""

    def __init__(self, declared_extension: str, head: bytes):
        self.declared_extension = declared_extension
        self.head = head
        super().__init__(f"declared extension {declared_extension!r} but downloaded bytes start with {head!r}")


def _check_download_integrity(name: str | None, content: bytes) -> None:
    if not name or "." not in name:
        return
    extension = "." + name.rsplit(".", 1)[1].lower()
    expected_magic = _EXPECTED_MAGIC_BY_EXTENSION.get(extension)
    if expected_magic is None or content.startswith(expected_magic):
        return
    raise DownloadIntegrityError(extension, content[:32])

# Native Google Workspace types have no raw bytes - they must be exported to
# a concrete format instead of downloaded directly. Not exhaustive (Slides
# and anything unlisted falls back to a plain-text export attempt) -
# covering every Workspace type isn't worth it here.
_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_DEFAULT_EXPORT_MIME_TYPE = "text/plain"

_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _parse_rfc3339(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class GoogleDriveCollector(SaaSCollector):
    """Real Google Drive sync via google-api-python-client (Drive v3).

    Initial sync lists + downloads every file (optionally scoped to
    `folder_id`). Incremental sync uses the Drive Changes API
    (`changes().list`), which natively reports deletions via
    `change.removed` - preferred over `modifiedTime` filtering as the class
    docstring originally noted.
    """

    source_type = SourceType.GOOGLE_DRIVE

    def __init__(
        self,
        client: Any,
        folder_id: str | None = None,
        auth_config: SaaSAuthConfig | None = None,
    ):
        super().__init__(client=client, auth_config=auth_config)
        self._folder_id = folder_id
        self._start_page_token: str | None = None

    def _download_content(self, file: dict[str, Any]) -> bytes:
        mime_type = file.get("mimeType", "")
        if mime_type.startswith("application/vnd.google-apps."):
            export_mime_type = _EXPORT_MIME_TYPES.get(mime_type, _DEFAULT_EXPORT_MIME_TYPE)
            return self._client.files().export_media(fileId=file["id"], mimeType=export_mime_type).execute()
        content = self._client.files().get_media(fileId=file["id"]).execute()
        _check_download_integrity(file.get("name"), content)
        return content

    def _raw_item_for_file(self, file: dict[str, Any]) -> RawItem | None:
        """Returns None (a collection failure, logged here) when the
        downloaded bytes don't match what the file claims to be - see
        DownloadIntegrityError. Callers skip a None the same way they
        already skip a folder entry, rather than letting a corrupt file
        turn into a Source/content_hash that quietly blesses bad bytes."""
        uri = file.get("webViewLink") or f"gdrive://{file['id']}"
        try:
            content = self._download_content(file)
        except DownloadIntegrityError as exc:
            logger.error(
                "gdrive download integrity check failed, skipping %s (declared type=%s, name=%r): head=%r",
                uri,
                exc.declared_extension,
                file.get("name"),
                exc.head,
            )
            return None
        return RawItem(
            uri=uri,
            content=content,
            content_hash=compute_content_hash(content),
            modified_at=_parse_rfc3339(file["modifiedTime"]),
            metadata={"file_id": file["id"], "name": file.get("name"), "mimeType": file.get("mimeType")},
            deleted=False,
        )

    def _refresh_start_page_token(self) -> None:
        response = self._client.changes().getStartPageToken().execute()
        self._start_page_token = response.get("startPageToken")

    def _list_files(self, query: str | None) -> Iterator[dict]:
        def request_fn(cursor: str | None) -> tuple[list[dict], str | None]:
            response = self._client.files().list(q=query, pageToken=cursor, fields=_LIST_FIELDS).execute()
            return response.get("files", []), response.get("nextPageToken")

        yield from self._paginate(request_fn)

    def initial_sync(self) -> Iterator[RawItem]:
        # `'{id}' in parents` only lists direct children (Drive has no
        # recursive-list query), so a scoped folder_id needs a BFS walk of
        # its subfolders - folders themselves have no downloadable content
        # and must never be passed to export/get_media. With no folder_id,
        # `files().list(q=None)` already returns every file the service
        # account can see in one flat (non-recursive) pass, so a folder
        # entry there is just skipped rather than re-queried - its children
        # are already present in that same flat listing, and recursing
        # into it would yield them a second time.
        pending_folder_ids: list[str | None] = [self._folder_id] if self._folder_id else [None]

        while pending_folder_ids:
            parent_id = pending_folder_ids.pop(0)
            query = f"'{parent_id}' in parents" if parent_id else None
            for file in self._list_files(query):
                if file.get("mimeType") == _FOLDER_MIME_TYPE:
                    if parent_id is not None:
                        pending_folder_ids.append(file["id"])
                    continue
                item = self._raw_item_for_file(file)
                if item is not None:
                    yield item

        self._refresh_start_page_token()

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        start_page_token = cursor.data["start_page_token"]
        new_start_page_token: str | None = None

        def request_fn(page_cursor: str | None) -> tuple[list[dict], str | None]:
            nonlocal new_start_page_token
            response = self._client.changes().list(
                pageToken=page_cursor or start_page_token, fields=_CHANGES_FIELDS
            ).execute()
            if response.get("newStartPageToken"):
                new_start_page_token = response["newStartPageToken"]
            return response.get("changes", []), response.get("nextPageToken")

        for change in self._paginate(request_fn):
            if change.get("removed"):
                yield RawItem(
                    uri=f"gdrive://{change['fileId']}",
                    content=b"",
                    content_hash="",
                    modified_at=datetime.now(tz=timezone.utc),
                    metadata={"file_id": change["fileId"]},
                    deleted=True,
                )
            else:
                item = self._raw_item_for_file(change["file"])
                if item is not None:
                    yield item

        self._start_page_token = new_start_page_token or start_page_token

    def get_cursor(self) -> Cursor:
        return Cursor(data={"start_page_token": self._start_page_token})
