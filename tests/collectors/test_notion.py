from knowledge_forge.collectors.base import Cursor
from knowledge_forge.collectors.saas.notion import NotionCollector


def _rich_text(text: str) -> list[dict]:
    return [{"plain_text": text}]


def _page(page_id: str, last_edited: str, title: str = "Untitled") -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "last_edited_time": last_edited,
        "properties": {"Name": {"type": "title", "title": _rich_text(title)}},
    }


class _FakeBlocksChildren:
    def __init__(self, blocks_by_id: dict[str, list[dict]]):
        self._blocks_by_id = blocks_by_id
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        block_id = kwargs["block_id"]
        return {"results": self._blocks_by_id.get(block_id, []), "has_more": False, "next_cursor": None}


class _FakeBlocks:
    def __init__(self, blocks_by_id: dict[str, list[dict]]):
        self.children = _FakeBlocksChildren(blocks_by_id)


class _FakeDatabases:
    def __init__(self, pages_by_db: dict[str, list[dict]]):
        self._pages_by_db = pages_by_db
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": self._pages_by_db.get(kwargs["database_id"], []), "has_more": False, "next_cursor": None}


class _FakeNotionClient:
    def __init__(self, pages: list[dict], blocks_by_id: dict[str, list[dict]] | None = None, pages_by_db: dict | None = None):
        self._pages = pages
        self.blocks = _FakeBlocks(blocks_by_id or {})
        self.databases = _FakeDatabases(pages_by_db or {})
        self.search_calls: list[dict] = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return {"results": self._pages, "has_more": False, "next_cursor": None}


def test_initial_sync_flattens_block_text_and_extracts_title():
    pages = [_page("p1", "2024-01-01T00:00:00.000Z", title="Page One")]
    blocks = {
        "p1": [
            {"id": "b1", "type": "paragraph", "paragraph": {"rich_text": _rich_text("first line")}, "has_children": False},
            {"id": "b2", "type": "heading_1", "heading_1": {"rich_text": _rich_text("a heading")}, "has_children": True},
        ],
        "b2": [
            {"id": "b3", "type": "paragraph", "paragraph": {"rich_text": _rich_text("nested line")}, "has_children": False},
        ],
    }
    client = _FakeNotionClient(pages, blocks_by_id=blocks)
    collector = NotionCollector(client=client)

    items = list(collector.initial_sync())

    assert len(items) == 1
    assert items[0].content.decode("utf-8") == "first line\na heading\nnested line"
    assert items[0].uri == "https://notion.so/p1"
    assert items[0].metadata["title"] == "Page One"

    cursor = collector.get_cursor()
    assert cursor.data["known_page_ids"] == {"p1": "2024-01-01T00:00:00.000Z"}
    assert cursor.data["last_synced_at"] == "2024-01-01T00:00:00.000Z"


def test_databases_query_used_when_database_id_given():
    pages_by_db = {"db1": [_page("p1", "2024-01-01T00:00:00.000Z")]}
    client = _FakeNotionClient(pages=[], pages_by_db=pages_by_db)
    collector = NotionCollector(client=client, database_id="db1")

    items = list(collector.initial_sync())

    assert len(items) == 1
    assert client.databases.calls[0]["database_id"] == "db1"
    assert client.search_calls == []


def test_incremental_sync_yields_only_newer_pages_and_detects_deletions():
    newer_page = _page("p_new", "2024-02-01T00:00:00.000Z")
    unchanged_page = _page("p_old", "2024-01-01T00:00:00.000Z")
    client = _FakeNotionClient(pages=[newer_page, unchanged_page])
    collector = NotionCollector(client=client)

    cursor = Cursor(
        data={
            "last_synced_at": "2024-01-01T00:00:00.000Z",
            "known_page_ids": {
                "p_old": "2024-01-01T00:00:00.000Z",
                "p_gone": "2023-12-01T00:00:00.000Z",
            },
        }
    )

    items = list(collector.incremental_sync(cursor))
    by_page_id = {item.metadata.get("page_id"): item for item in items}

    assert "p_new" in by_page_id
    assert not by_page_id["p_new"].deleted
    assert "p_old" not in by_page_id  # unchanged - not yielded

    deleted = [i for i in items if i.deleted]
    assert len(deleted) == 1
    assert deleted[0].metadata["page_id"] == "p_gone"

    new_cursor = collector.get_cursor()
    assert new_cursor.data["known_page_ids"] == {
        "p_new": "2024-02-01T00:00:00.000Z",
        "p_old": "2024-01-01T00:00:00.000Z",
    }
    assert new_cursor.data["last_synced_at"] == "2024-02-01T00:00:00.000Z"
