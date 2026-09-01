"""OKF: a file-based, manifest-indexed Knowledge Base tree.

This is the "Organization Views / Manifest" layer described in
wiki/architecture.md - a second, optional output alongside (not instead
of) the PostgreSQL PipelineStore. PipelineStore is the queryable store;
OKFWriter is a portable, git-diffable, human-readable file tree of the
same data that needs no database to exist or be inspected - useful as the
"source of truth" file format a Graph DB/search index can later be
materialized from without a pipeline redesign (see the excluded-scope
table in wiki/roadmap.md).

Layout:

    <root>/{project_id}/
      manifest.yaml
      resources/{source_id}.yaml           # Source + Document (the source-native unit)
      objects/{type}/{knowledge_object_id}.yaml
      relations/{relation_id}.yaml
      views/by_topic.yaml                  # topic -> [{id, type, path}]
      views/by_document.yaml               # document_id -> {title, objects: [...]}
      views/timeline.yaml                  # [{id, type, path, extracted_at}], chronological

Every writer is scoped to one project_id's subtree (`<root>/{project_id}/`)
so each project's OKF tree can be located, git-diffed, or copied out on its
own - the manifest/views a `write_manifest()`/`write_views()` call rebuilds
are that project's alone, not a mix of every project ever written under
`<root>`.

The manifest is a catalog, not a knowledge store in itself: for every
resource/object/relation it holds just id + type + the relative path to
the full record, plus generated_at/pipeline_version. It is rebuilt by
scanning the tree (not accumulated in memory) so it stays correct across
multiple pipeline runs writing into the same root (e.g. incremental
syncs) - each run only needs to write its own new/changed records, then
call `write_manifest()` once at the end to re-catalog everything present.

`views/` (wiki/roadmap.md 5단계 item 3) are the same idea one layer up:
alternate groupings of the same `objects/{type}/...` records - by topic,
by document (the closest addressable stand-in for "project": there is no
separate Project concept in the data model, see
models/knowledge_object.py's DocumentRef), and chronologically - built by
reading every object record's `topics`/`document`/`extracted_at` fields,
not a new record format. Like the manifest, `write_views()` rebuilds by
scanning `objects/`, so it stays correct across incremental runs the same
way `write_manifest()` does.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import knowledge_forge
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import KnowledgeObject
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source

_RESOURCES_DIR = "resources"
_OBJECTS_DIR = "objects"
_RELATIONS_DIR = "relations"
_VIEWS_DIR = "views"


class OKFWriter:
    def __init__(self, root: Path | str, project_id: str = "default"):
        self._root = Path(root) / project_id
        for sub in (_RESOURCES_DIR, _OBJECTS_DIR, _RELATIONS_DIR, _VIEWS_DIR):
            (self._root / sub).mkdir(parents=True, exist_ok=True)

    def write_resource(self, source: Source, document: Document) -> None:
        """One resource file per Source, holding the Source record and a
        thin pointer to its Document (not the full element tree - that
        stays in the pipeline's own storage; the resource file's job is to
        be a stable, addressable "this is where object X came from")."""
        payload = {
            "source": source.model_dump(mode="json"),
            "document": {
                "document_id": document.document_id,
                "title": document.metadata.title,
                "element_count": len(document.elements),
            },
        }
        self._write_yaml(f"{_RESOURCES_DIR}/{source.source_id}.yaml", payload)

    def write_objects(self, objects: list[KnowledgeObject]) -> None:
        for ko in objects:
            self._write_yaml(
                f"{_OBJECTS_DIR}/{ko.type.value}/{ko.knowledge_object_id}.yaml",
                ko.model_dump(mode="json"),
            )

    def write_relations(self, relations: list[Relation]) -> None:
        for rel in relations:
            self._write_yaml(f"{_RELATIONS_DIR}/{rel.relation_id}.yaml", rel.model_dump(mode="json"))

    def write_manifest(self) -> Path:
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline_version": knowledge_forge.__version__,
            "resources": self._catalog_resources(),
            "objects": self._catalog_objects(),
            "relations": self._catalog_relations(),
        }
        path = self._root / "manifest.yaml"
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False)
        return path

    def write_views(self) -> list[Path]:
        by_topic: dict[str, list[dict[str, Any]]] = {}
        by_document: dict[str, dict[str, Any]] = {}
        timeline: list[dict[str, Any]] = []

        objects_dir = self._root / _OBJECTS_DIR
        for type_dir in sorted(d for d in objects_dir.iterdir() if d.is_dir()):
            for f in sorted(type_dir.glob("*.yaml")):
                payload = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                entry = {"id": payload.get("knowledge_object_id", f.stem), "type": type_dir.name, "path": self._relpath(f)}

                for topic in payload.get("topics") or []:
                    by_topic.setdefault(topic, []).append(entry)

                document = payload.get("document") or {}
                document_id = document.get("document_id")
                if document_id:
                    bucket = by_document.setdefault(document_id, {"title": document.get("title"), "objects": []})
                    bucket["objects"].append(entry)

                extracted_at = payload.get("extracted_at")
                if extracted_at:
                    timeline.append({**entry, "extracted_at": extracted_at})

        timeline.sort(key=lambda e: e["extracted_at"])

        paths = [
            self._write_yaml(f"{_VIEWS_DIR}/by_topic.yaml", dict(sorted(by_topic.items()))),
            self._write_yaml(f"{_VIEWS_DIR}/by_document.yaml", dict(sorted(by_document.items()))),
            self._write_yaml(f"{_VIEWS_DIR}/timeline.yaml", {"timeline": timeline}),
        ]
        return paths

    def _catalog_resources(self) -> list[dict[str, Any]]:
        entries = []
        for f in sorted((self._root / _RESOURCES_DIR).glob("*.yaml")):
            payload = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            source = payload.get("source", {})
            entries.append(
                {
                    "id": source.get("source_id", f.stem),
                    "type": source.get("source_type", "unknown"),
                    "path": self._relpath(f),
                }
            )
        return entries

    def _catalog_objects(self) -> list[dict[str, Any]]:
        entries = []
        objects_dir = self._root / _OBJECTS_DIR
        for type_dir in sorted(d for d in objects_dir.iterdir() if d.is_dir()):
            for f in sorted(type_dir.glob("*.yaml")):
                entries.append({"id": f.stem, "type": type_dir.name, "path": self._relpath(f)})
        return entries

    def _catalog_relations(self) -> list[dict[str, Any]]:
        entries = []
        for f in sorted((self._root / _RELATIONS_DIR).glob("*.yaml")):
            payload = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            entries.append({"id": f.stem, "type": payload.get("type", "unknown"), "path": self._relpath(f)})
        return entries

    def _write_yaml(self, rel_path: str, payload: dict) -> Path:
        path = self._root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        return path

    def _relpath(self, path: Path) -> str:
        return str(path.relative_to(self._root))
