"""build_*_collector() reads KF_-prefixed settings instead of requiring the
caller to hand-construct an SDK client. Each SDK is faked via sys.modules
injection so these tests don't need slack-sdk/notion-client/google-api-
python-client actually installed (they're optional extras).
"""

import json
import sys
import types

import pytest

from knowledge_forge.collectors.saas import factory
from knowledge_forge.collectors.saas.gdrive import GoogleDriveCollector
from knowledge_forge.collectors.saas.gmail import GmailCollector
from knowledge_forge.collectors.saas.notion import NotionCollector
from knowledge_forge.collectors.saas.slack import SlackCollector
from knowledge_forge.config import Settings


def _patch_settings(monkeypatch, **overrides):
    # _env_file=None: without this, Settings(**overrides) still falls back to
    # the real project-root .env for any field not in `overrides` - so a
    # developer's real KF_SLACK_BOT_TOKEN etc. silently leaks into these
    # "requires_X" tests and they stop raising.
    monkeypatch.setattr(factory, "settings", Settings(_env_file=None, **overrides))


# ─── Slack ───────────────────────────────────────────────────────────────


def test_build_slack_collector_requires_bot_token(monkeypatch):
    _patch_settings(monkeypatch, slack_channels="C1")
    with pytest.raises(RuntimeError, match="KF_SLACK_BOT_TOKEN"):
        factory.build_slack_collector()


def test_build_slack_collector_requires_channels(monkeypatch):
    _patch_settings(monkeypatch, slack_bot_token="xoxb-fake")
    with pytest.raises(RuntimeError, match="KF_SLACK_CHANNELS"):
        factory.build_slack_collector()


def test_build_slack_collector_builds_client_from_settings(monkeypatch):
    _patch_settings(monkeypatch, slack_bot_token="xoxb-fake", slack_channels="C1, C2")

    created_with = {}

    class _FakeWebClient:
        def __init__(self, token):
            created_with["token"] = token

    fake_module = types.ModuleType("slack_sdk")
    fake_module.WebClient = _FakeWebClient
    monkeypatch.setitem(sys.modules, "slack_sdk", fake_module)

    collector = factory.build_slack_collector()
    assert isinstance(collector, SlackCollector)
    assert collector._channels == ["C1", "C2"]
    assert created_with["token"] == "xoxb-fake"


# ─── Notion ──────────────────────────────────────────────────────────────


def test_build_notion_collector_requires_token(monkeypatch):
    _patch_settings(monkeypatch)
    with pytest.raises(RuntimeError, match="KF_NOTION_TOKEN"):
        factory.build_notion_collector()


def test_build_notion_collector_builds_client_from_settings(monkeypatch):
    _patch_settings(monkeypatch, notion_token="secret_fake", notion_database_id="db-1")

    created_with = {}

    class _FakeClient:
        def __init__(self, auth):
            created_with["auth"] = auth

    fake_module = types.ModuleType("notion_client")
    fake_module.Client = _FakeClient
    monkeypatch.setitem(sys.modules, "notion_client", fake_module)

    collector = factory.build_notion_collector()
    assert isinstance(collector, NotionCollector)
    assert collector._database_id == "db-1"
    assert created_with["auth"] == "secret_fake"


# ─── Google Drive ────────────────────────────────────────────────────────


def _install_fake_google_drive_sdk(monkeypatch, credentials_holder):
    class _FakeCredentials:
        @staticmethod
        def from_service_account_info(info, scopes):
            credentials_holder["info"] = info
            credentials_holder["scopes"] = scopes
            return "fake-drive-credentials"

    service_account_module = types.ModuleType("google.oauth2.service_account")
    service_account_module.Credentials = _FakeCredentials
    oauth2_module = types.ModuleType("google.oauth2")
    oauth2_module.service_account = service_account_module
    google_module = types.ModuleType("google")
    google_module.oauth2 = oauth2_module

    discovery_module = types.ModuleType("googleapiclient.discovery")

    def _fake_build(service_name, version, credentials):
        credentials_holder["build_args"] = (service_name, version, credentials)
        return "fake-drive-client"

    discovery_module.build = _fake_build
    googleapiclient_module = types.ModuleType("googleapiclient")
    googleapiclient_module.discovery = discovery_module

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", service_account_module)
    monkeypatch.setitem(sys.modules, "googleapiclient", googleapiclient_module)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery_module)


def test_build_gdrive_collector_requires_credentials(monkeypatch):
    _patch_settings(monkeypatch)
    with pytest.raises(RuntimeError, match="KF_GDRIVE_CREDENTIALS_JSON"):
        factory.build_gdrive_collector()


def test_build_gdrive_collector_accepts_inline_json(monkeypatch):
    creds_json = json.dumps({"type": "service_account", "project_id": "p"})
    _patch_settings(monkeypatch, gdrive_credentials_json=creds_json, gdrive_folder_id="folder-1")
    holder: dict = {}
    _install_fake_google_drive_sdk(monkeypatch, holder)

    collector = factory.build_gdrive_collector()
    assert isinstance(collector, GoogleDriveCollector)
    assert collector._folder_id == "folder-1"
    assert holder["info"]["project_id"] == "p"


def test_build_gdrive_collector_accepts_credentials_file_path(monkeypatch, tmp_path):
    creds_file = tmp_path / "service-account.json"
    creds_file.write_text(json.dumps({"type": "service_account", "project_id": "from-file"}))
    _patch_settings(monkeypatch, gdrive_credentials_json=str(creds_file))
    holder: dict = {}
    _install_fake_google_drive_sdk(monkeypatch, holder)

    factory.build_gdrive_collector()
    assert holder["info"]["project_id"] == "from-file"


# ─── Gmail ───────────────────────────────────────────────────────────────


def _install_fake_gmail_sdk(monkeypatch, credentials_holder):
    class _FakeCredentials:
        expired = False
        refresh_token = None

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            credentials_holder["token_path"] = path
            credentials_holder["scopes"] = scopes
            return cls()

    credentials_module = types.ModuleType("google.oauth2.credentials")
    credentials_module.Credentials = _FakeCredentials
    oauth2_module = types.ModuleType("google.oauth2")
    oauth2_module.credentials = credentials_module
    google_module = types.ModuleType("google")
    google_module.oauth2 = oauth2_module

    auth_transport_requests_module = types.ModuleType("google.auth.transport.requests")
    auth_transport_requests_module.Request = object
    auth_transport_module = types.ModuleType("google.auth.transport")
    auth_transport_module.requests = auth_transport_requests_module
    auth_module = types.ModuleType("google.auth")
    auth_module.transport = auth_transport_module
    google_module.auth = auth_module

    discovery_module = types.ModuleType("googleapiclient.discovery")

    def _fake_build(service_name, version, credentials):
        credentials_holder["build_args"] = (service_name, version, credentials)
        return "fake-gmail-client"

    discovery_module.build = _fake_build
    googleapiclient_module = types.ModuleType("googleapiclient")
    googleapiclient_module.discovery = discovery_module

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_module)
    monkeypatch.setitem(sys.modules, "google.auth", auth_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport", auth_transport_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", auth_transport_requests_module)
    monkeypatch.setitem(sys.modules, "googleapiclient", googleapiclient_module)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery_module)


def test_build_gmail_collector_requires_token_path(monkeypatch):
    _patch_settings(monkeypatch)
    with pytest.raises(RuntimeError, match="KF_GMAIL_TOKEN_PATH"):
        factory.build_gmail_collector()


def test_build_gmail_collector_requires_token_file_to_exist(monkeypatch, tmp_path):
    missing = tmp_path / "token.json"
    _patch_settings(monkeypatch, gmail_token_path=str(missing))
    with pytest.raises(RuntimeError, match="does not exist"):
        factory.build_gmail_collector()


def test_build_gmail_collector_builds_client_from_existing_token(monkeypatch, tmp_path):
    token_file = tmp_path / "token.json"
    token_file.write_text("{}")
    _patch_settings(monkeypatch, gmail_token_path=str(token_file), gmail_query="label:x")
    holder: dict = {}
    _install_fake_gmail_sdk(monkeypatch, holder)

    collector = factory.build_gmail_collector()
    assert isinstance(collector, GmailCollector)
    assert collector._query == "label:x"
    assert holder["token_path"] == str(token_file)
