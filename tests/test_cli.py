"""_build_collector() is the seam extract/schedule use to pick a Collector
via --source (wiki/roadmap.md 1단계) - "local" (default) needs PATH and
returns a LocalFileCollector, anything else delegates to
collectors/saas/factory.py's build_*_collector() and ignores PATH.
"""

import pytest
import typer

from knowledge_forge import cli
from knowledge_forge.collectors.local_file import LocalFileCollector


def test_local_source_requires_path():
    with pytest.raises(typer.Exit):
        cli._build_collector("local", None)


def test_local_source_returns_local_file_collector(tmp_path):
    target = tmp_path / "doc.md"
    target.write_text("hello")

    collector = cli._build_collector("local", target)

    assert isinstance(collector, LocalFileCollector)


def test_unknown_source_exits():
    with pytest.raises(typer.Exit):
        cli._build_collector("carrier-pigeon", None)


def test_saas_source_delegates_to_matching_factory_builder(monkeypatch):
    from knowledge_forge.collectors.saas import factory

    sentinel = object()
    monkeypatch.setattr(factory, "build_slack_collector", lambda: sentinel)

    assert cli._build_collector("slack", None) is sentinel
