"""Builds authenticated SaaS collectors from `KF_`-prefixed env config
(knowledge_forge.config.settings) instead of requiring callers to construct
an SDK client + Collector by hand. Mirrors vision/factory.py's shape.

Each `build_*_collector()` lazily imports its SDK (so an unused extra never
needs to be installed) and raises RuntimeError with a specific message when
required settings are missing.
"""

import json
from pathlib import Path
from typing import Callable

from knowledge_forge.collectors.base import Collector
from knowledge_forge.collectors.saas.base import SaaSAuthConfig
from knowledge_forge.collectors.saas.gdrive import GoogleDriveCollector
from knowledge_forge.collectors.saas.gmail import GmailCollector
from knowledge_forge.collectors.saas.notion import NotionCollector
from knowledge_forge.collectors.saas.slack import SlackCollector
from knowledge_forge.config import settings


def _load_json_config(value: str) -> dict:
    """`value` is either an inline JSON object or a path to a file containing one."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return json.loads(Path(value).read_text())


def build_slack_collector() -> SlackCollector:
    if not settings.slack_bot_token:
        raise RuntimeError("KF_SLACK_BOT_TOKEN is not set.")

    channel_project_map: dict[str, str] = {}
    if settings.slack_channel_project_map:
        channel_project_map = _load_json_config(settings.slack_channel_project_map)

    if settings.slack_channels:
        channels = [c.strip() for c in settings.slack_channels.split(",") if c.strip()]
    elif channel_project_map:
        channels = list(channel_project_map)
    else:
        raise RuntimeError(
            "Neither KF_SLACK_CHANNELS nor KF_SLACK_CHANNEL_PROJECT_MAP is set "
            "(comma-separated channel IDs, or a channel-id -> project-id JSON map)."
        )

    from slack_sdk import WebClient

    client = WebClient(token=settings.slack_bot_token)
    return SlackCollector(
        client=client,
        channels=channels,
        auth_config=SaaSAuthConfig(token=settings.slack_bot_token),
        channel_project_map=channel_project_map or None,
    )


def build_notion_collector() -> NotionCollector:
    if not settings.notion_token:
        raise RuntimeError("KF_NOTION_TOKEN is not set.")

    from notion_client import Client

    client = Client(auth=settings.notion_token)
    return NotionCollector(
        client=client,
        database_id=settings.notion_database_id,
        auth_config=SaaSAuthConfig(token=settings.notion_token),
    )


def build_gdrive_collector() -> GoogleDriveCollector:
    if not settings.gdrive_credentials_json:
        raise RuntimeError("KF_GDRIVE_CREDENTIALS_JSON is not set.")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = _load_json_config(settings.gdrive_credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    client = build("drive", "v3", credentials=credentials)
    return GoogleDriveCollector(client=client, folder_id=settings.gdrive_folder_id)


def build_gmail_collector() -> GmailCollector:
    if not settings.gmail_token_path:
        raise RuntimeError(
            "KF_GMAIL_TOKEN_PATH is not set. Gmail needs a pre-authorized user OAuth "
            "token (KF_GMAIL_CREDENTIALS_PATH is only the client config used to obtain it)."
        )

    token_path = Path(settings.gmail_token_path)
    if not token_path.exists():
        raise RuntimeError(
            f"KF_GMAIL_TOKEN_PATH ({token_path}) does not exist. Run the Gmail OAuth "
            "setup once (using KF_GMAIL_CREDENTIALS_PATH) to produce it."
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    credentials = Credentials.from_authorized_user_file(str(token_path), scopes)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        token_path.write_text(credentials.to_json())

    client = build("gmail", "v1", credentials=credentials)
    return GmailCollector(client=client, query=settings.gmail_query)


# Canonical source-name -> builder mapping, shared by every caller that lets
# an operator pick a SaaS source by name instead of importing a specific
# build_*_collector() (cli.py's --source, api/routes/ingest.py's `source`
# field, worker.py's `{source}_ingest` job dispatch) - one place to extend
# when a new SaaS collector is added.
SAAS_COLLECTOR_BUILDERS: dict[str, Callable[[], Collector]] = {
    "slack": build_slack_collector,
    "notion": build_notion_collector,
    "gdrive": build_gdrive_collector,
    "gmail": build_gmail_collector,
}
