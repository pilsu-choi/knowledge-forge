from datetime import datetime, timezone
from typing import Any, Iterator

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.ids import content_hash as compute_content_hash
from knowledge_forge.models.source import SourceType

_LIST_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)"
_CHANGES_FIELDS = "nextPageToken, newStartPageToken, changes(fileId, removed, file(id, name, mimeType, modifiedTime, webViewLink))"

# Native Google Workspace types have no raw bytes - they must be exported to
# a concrete format instead of downloaded directly. Not exhaustive (Slides
# and anything unlisted falls back to a plain-text export attempt) -
# covering every Workspace type isn't worth it here.
_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_DEFAULT_EXPORT_MIME_TYPE = "text/plain"


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
        return self._client.files().get_media(fileId=file["id"]).execute()

    def _raw_item_for_file(self, file: dict[str, Any]) -> RawItem:
        content = self._download_content(file)
        return RawItem(
            uri=file.get("webViewLink") or f"gdrive://{file['id']}",
            content=content,
            content_hash=compute_content_hash(content),
            modified_at=_parse_rfc3339(file["modifiedTime"]),
            metadata={"file_id": file["id"], "name": file.get("name"), "mimeType": file.get("mimeType")},
            deleted=False,
        )

    def _refresh_start_page_token(self) -> None:
        response = self._client.changes().getStartPageToken().execute()
        self._start_page_token = response.get("startPageToken")

    def initial_sync(self) -> Iterator[RawItem]:
        query = f"'{self._folder_id}' in parents" if self._folder_id else None

        def request_fn(cursor: str | None) -> tuple[list[dict], str | None]:
            response = self._client.files().list(q=query, pageToken=cursor, fields=_LIST_FIELDS).execute()
            return response.get("files", []), response.get("nextPageToken")

        for file in self._paginate(request_fn):
            yield self._raw_item_for_file(file)

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
                yield self._raw_item_for_file(change["file"])

        self._start_page_token = new_start_page_token or start_page_token

    def get_cursor(self) -> Cursor:
        return Cursor(data={"start_page_token": self._start_page_token})
