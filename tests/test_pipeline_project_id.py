"""RawItem.project_id override (roadmap: SlackCollector channel_project_map)
lets a single collector instance spanning several projects still tag each
Source correctly - see pipeline._make_source.
"""

from datetime import datetime, timezone

from knowledge_forge.collectors.base import RawItem
from knowledge_forge.models.source import SourceType
from knowledge_forge.pipeline import _make_source


class _FakeCollector:
    source_type = SourceType.SLACK


def _item(**overrides) -> RawItem:
    defaults = dict(
        uri="slack://C1/100.0",
        content=b"hi",
        content_hash="h",
        modified_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return RawItem(**defaults)


def test_item_level_project_id_overrides_the_run_level_default():
    source = _make_source(_FakeCollector(), _item(project_id="PRJ-A"), project_id="default")
    assert source.project_id == "PRJ-A"


def test_missing_item_level_project_id_falls_back_to_run_level_default():
    source = _make_source(_FakeCollector(), _item(), project_id="default")
    assert source.project_id == "default"
