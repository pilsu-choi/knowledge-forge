from typing import Any, Iterator

from knowledge_forge.collectors.base import Cursor, RawItem
from knowledge_forge.collectors.saas.base import SaaSAuthConfig, SaaSCollector
from knowledge_forge.models.source import SourceType


class GoogleDriveCollector(SaaSCollector):
    """Interface-only: real Google Drive sync is not implemented yet.

    Intended implementation (google-api-python-client Drive v3 service):
    initial sync calls `client.files().list(q=f"'{folder_id}' in parents",
    pageToken=..., fields="nextPageToken, files(id, name, mimeType,
    modifiedTime, webViewLink)")` (drive pagination through
    `SaaSCollector._paginate`), then downloads content per file via
    `client.files().get_media(fileId=...)` for binary files or
    `client.files().export_media(fileId=..., mimeType=...)` for native
    Google Docs/Sheets/Slides. Incremental sync filters the `q` clause on
    `modifiedTime > '<cursor timestamp>'`, or better, uses the Drive
    Changes API (`client.changes().list(pageToken=<saved start token>)`)
    which natively reports deletions via `change.removed` - preferred over
    `modifiedTime` filtering once implemented.
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

    def initial_sync(self) -> Iterator[RawItem]:
        raise NotImplementedError(
            "GoogleDriveCollector.initial_sync: implement via files().list(...) + "
            "files().get_media()/export_media(). See class docstring."
        )

    def incremental_sync(self, cursor: Cursor) -> Iterator[RawItem]:
        raise NotImplementedError(
            "GoogleDriveCollector.incremental_sync: implement via changes().list("
            "pageToken=<start token>) for a native change/delete feed. See class docstring."
        )

    def get_cursor(self) -> Cursor:
        raise NotImplementedError(
            "GoogleDriveCollector.get_cursor: persist the Drive Changes API start "
            "page token as the cursor."
        )
