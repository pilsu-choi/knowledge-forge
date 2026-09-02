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
      links/{link_id}.yaml                 # resource-to-resource edges (wiki/okf.md)
      views/by_topic.yaml                  # topic -> [{id, type, path}]
      views/by_document.yaml               # document_id -> {title, objects: [...]}
      views/timeline.yaml                  # [{id, type, path, extracted_at}], chronological

Each project gets its own subtree (`<root>/{project_id}/`) so it can be
located, git-diffed, or copied out on its own. One OKFWriter instance can
still write into several project subtrees in the same run - every
`write_resource`/`write_objects` call routes to the project_id already
carried by the Source/KnowledgeObject it's writing (`Source.project_id`,
`KnowledgeObject.source.project_id` - see pipeline.py's per-item
`RawItem.project_id` override, e.g. one Slack collector spanning several
projects' channels), not a single project_id fixed at construction time.
`write_relations()` takes the project_id explicitly (a Relation carries no
Source of its own) - pipeline.py passes the enclosing document's
`source.project_id`. `write_manifest()`/`write_views()` rebuild every
project subtree found under `<root>` (each still scoped to its own
records, never mixed with another project's), returned as
`{project_id: path}` (or `{project_id: [paths]}` for views) since one run
can touch more than one.

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
from knowledge_forge.models.link import Link
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source

_RESOURCES_DIR = "resources"
_OBJECTS_DIR = "objects"
_RELATIONS_DIR = "relations"
_LINKS_DIR = "links"
_VIEWS_DIR = "views"


class OKFWriter:
    def __init__(self, root: Path | str, project_id: str = "default"):
        self._base_root = Path(root)
        # `project_id` is the fallback used by write_relations() (a Relation
        # carries no Source of its own to route by) and ensures at least one
        # project subtree exists even for a writer that never writes a
        # single item - matches the old always-one-project behavior.
        self._default_project_id = project_id
        self._project_root(project_id)

    def _project_root(self, project_id: str) -> Path:
        root = self._base_root / project_id
        for sub in (_RESOURCES_DIR, _OBJECTS_DIR, _RELATIONS_DIR, _LINKS_DIR, _VIEWS_DIR):
            (root / sub).mkdir(parents=True, exist_ok=True)
        return root

    def _known_project_ids(self) -> list[str]:
        """Every project subtree that exists under root - discovered by
        scanning the filesystem (like write_manifest()/write_views()
        already did per-project, see module docstring) rather than tracked
        in memory, so a manifest/views rebuild sees every project a prior
        OKFWriter instance wrote too, not just this one's."""
        if not self._base_root.is_dir():
            return [self._default_project_id]
        ids = sorted(d.name for d in self._base_root.iterdir() if d.is_dir())
        return ids or [self._default_project_id]

    def write_resource(self, source: Source, document: Document) -> None:
        """One resource file per Source, holding the Source record and a
        thin pointer to its Document (not the full element tree - that
        stays in the pipeline's own storage; the resource file's job is to
        be a stable, addressable "this is where object X came from")."""
        root = self._project_root(source.project_id)
        payload = {
            "source": source.model_dump(mode="json"),
            "document": {
                "document_id": document.document_id,
                "title": document.metadata.title,
                "element_count": len(document.elements),
            },
        }
        self._write_yaml(root, f"{_RESOURCES_DIR}/{source.source_id}.yaml", payload)

    def write_objects(self, objects: list[KnowledgeObject]) -> None:
        for ko in objects:
            root = self._project_root(ko.source.project_id)
            self._write_yaml(
                root,
                f"{_OBJECTS_DIR}/{ko.type.value}/{ko.knowledge_object_id}.yaml",
                ko.model_dump(mode="json"),
            )

    def write_relations(self, relations: list[Relation], project_id: str | None = None) -> None:
        """`project_id` is required to route correctly whenever a run spans
        more than one project - a Relation only holds KnowledgeObject ids
        (subject_id/object_id), not a Source, so it can't be routed by its
        own content the way write_objects() routes by
        `KnowledgeObject.source.project_id`. Defaults to the writer's
        construction-time project_id for callers with a single project.
        """
        root = self._project_root(project_id or self._default_project_id)
        for rel in relations:
            self._write_yaml(root, f"{_RELATIONS_DIR}/{rel.relation_id}.yaml", rel.model_dump(mode="json"))

    def write_links(self, links: list[Link], project_id: str | None = None) -> None:
        """Resource-level cross-document edges (wiki/okf.md's "그래프 뷰어
        대비" decision) - same routing rationale as write_relations(): a
        Link's endpoints are (source_id, document_id) pairs, not a Source of
        its own, so it can't route itself by content."""
        root = self._project_root(project_id or self._default_project_id)
        for link in links:
            self._write_yaml(root, f"{_LINKS_DIR}/{link.link_id}.yaml", link.model_dump(mode="json"))

    def write_manifest(self) -> dict[str, Path]:
        return {project_id: self._write_manifest_for(project_id) for project_id in self._known_project_ids()}

    def write_views(self) -> dict[str, list[Path]]:
        return {project_id: self._write_views_for(project_id) for project_id in self._known_project_ids()}

    def _write_manifest_for(self, project_id: str) -> Path:
        root = self._project_root(project_id)
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline_version": knowledge_forge.__version__,
            "resources": self._catalog_resources(root),
            "objects": self._catalog_objects(root),
            "relations": self._catalog_relations(root),
            "links": self._catalog_links(root),
        }
        path = root / "manifest.yaml"
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False)
        return path

    def _write_views_for(self, project_id: str) -> list[Path]:
        root = self._project_root(project_id)
        by_topic: dict[str, list[dict[str, Any]]] = {}
        by_document: dict[str, dict[str, Any]] = {}
        timeline: list[dict[str, Any]] = []

        objects_dir = root / _OBJECTS_DIR
        for type_dir in sorted(d for d in objects_dir.iterdir() if d.is_dir()):
            for f in sorted(type_dir.glob("*.yaml")):
                payload = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                entry = {
                    "id": payload.get("knowledge_object_id", f.stem),
                    "type": type_dir.name,
                    "path": self._relpath(root, f),
                }

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

        return [
            self._write_yaml(root, f"{_VIEWS_DIR}/by_topic.yaml", dict(sorted(by_topic.items()))),
            self._write_yaml(root, f"{_VIEWS_DIR}/by_document.yaml", dict(sorted(by_document.items()))),
            self._write_yaml(root, f"{_VIEWS_DIR}/timeline.yaml", {"timeline": timeline}),
        ]

    def _catalog_resources(self, root: Path) -> list[dict[str, Any]]:
        entries = []
        for f in sorted((root / _RESOURCES_DIR).glob("*.yaml")):
            payload = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            source = payload.get("source", {})
            entries.append(
                {
                    "id": source.get("source_id", f.stem),
                    "type": source.get("source_type", "unknown"),
                    "path": self._relpath(root, f),
                }
            )
        return entries

    def _catalog_objects(self, root: Path) -> list[dict[str, Any]]:
        entries = []
        objects_dir = root / _OBJECTS_DIR
        for type_dir in sorted(d for d in objects_dir.iterdir() if d.is_dir()):
            for f in sorted(type_dir.glob("*.yaml")):
                entries.append({"id": f.stem, "type": type_dir.name, "path": self._relpath(root, f)})
        return entries

    def _catalog_relations(self, root: Path) -> list[dict[str, Any]]:
        entries = []
        for f in sorted((root / _RELATIONS_DIR).glob("*.yaml")):
            payload = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            entries.append({"id": f.stem, "type": payload.get("type", "unknown"), "path": self._relpath(root, f)})
        return entries

    def _catalog_links(self, root: Path) -> list[dict[str, Any]]:
        entries = []
        for f in sorted((root / _LINKS_DIR).glob("*.yaml")):
            payload = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            entries.append({"id": f.stem, "type": payload.get("type", "unknown"), "path": self._relpath(root, f)})
        return entries

    def _write_yaml(self, root: Path, rel_path: str, payload: dict) -> Path:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        return path

    def _relpath(self, root: Path, path: Path) -> str:
        return str(path.relative_to(root))
