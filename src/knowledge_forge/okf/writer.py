"""OKF: a file-based, spec-conformant Knowledge Base tree.

This is the "Organization Views / Manifest" layer described in
wiki/architecture.md - a second, optional output alongside (not instead
of) the PostgreSQL PipelineStore. Unlike the earlier YAML-record version,
this writer targets actual OKF v0.2 conformance (wiki/open-knowledge-format.md):
every record is a UTF-8 markdown file with a YAML frontmatter block, meant
to be read by an agent with nothing but a filesystem and Grep - no
database, no vector store, no bespoke SDK. This is the concrete point of
the experiment: build agent context from the OKF tree alone.

Layout (per project subtree, each project is its own OKF bundle):

    <root>/{project_id}/
      index.md                             # Bundle root, progressive disclosure (spec S8)
      log.md                                # Chronological extraction history (spec S9)
      manifest.yaml                         # id -> path accelerator index, NOT a knowledge store
      resources/{slug}-{source_id8}.md      # Source+Document, a concept in its own right
      resources/index.md
      objects/{type}/{slug}-{ko_id8}.md     # KnowledgeObject as a concept document
      objects/{type}/index.md
      relations/{relation_id}.md            # ONLY orphan relations whose endpoints didn't resolve
      links/{link_id}.md                    # ONLY orphan links whose endpoints didn't resolve
      views/by-topic.md                     # topic -> concept list, a cached synthesis of tags

Why relations/links are usually *not* separate files any more: OKF v0.2 has
no first-class typed-edge record (S6.1 - edges are markdown links plus
prose). A `Relation`/`Link` carries a `type`/`confidence`/`label` that would
be lost if flattened into prose, so instead of inventing a new reserved
file, `write_relations()`/`write_links()` denormalize each edge onto BOTH
endpoint concept files as a `relations:`/`links:` frontmatter extension
entry (S4.1 explicitly allows producer-defined keys) plus a matching
"## Relations"/"## Links" body section rendered from that same frontmatter
list - so a single concept file answers "what does this mention" and
"what mentions this" without following a separate edge file. An edge whose
subject/object/source/target can't be resolved to an on-disk concept (rare
- see `_lookup_object`/`_lookup_resource`) falls back to its own file under
`relations/`/`links/` so the data is never silently dropped.

Concept filenames are `{slug}-{id8}.md`, not the bare id, so an agent can
`ls`/glob and get a sense of what's in a directory before grepping - the
whole point of doing this without a database is that browsing must work
from the filesystem alone.

Everything knowledge-forge-specific that OKF doesn't standardize
(`knowledge_object_id`, `evidence`, `model_used`, `version`/`supersedes`,
`source_hash`, ...) stays as frontmatter extension keys, per S4.1 ("MAY
include any additional keys"). `supersedes` additionally triggers a
lifecycle patch: the superseded concept's `status` flips to `deprecated`
(S5.4) the moment the newer object is written.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import knowledge_forge
from knowledge_forge.models.document import Document
from knowledge_forge.models.knowledge_object import KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.link import Link
from knowledge_forge.models.relation import Relation
from knowledge_forge.models.source import Source

_RESOURCES_DIR = "resources"
_OBJECTS_DIR = "objects"
_RELATIONS_DIR = "relations"
_LINKS_DIR = "links"
_VIEWS_DIR = "views"

_OKF_VERSION = "0.2"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_SLUG_WS_RE = re.compile(r"[\s_]+")
_SLUG_STRIP_RE = re.compile(r"[^\w\-]+", re.UNICODE)

_SOURCE_TYPE_TITLES = {
    "local_file": "Local File Source",
    "slack": "Slack Source",
    "gmail": "Gmail Source",
    "google_drive": "Google Drive Source",
    "notion": "Notion Source",
}


# --------------------------------------------------------------------------
# Markdown + frontmatter helpers
# --------------------------------------------------------------------------


def _slugify(text: str | None, max_len: int = 40) -> str:
    """Keeps non-Latin scripts (Korean in particular) intact - transliterating
    would make the corpus harder, not easier, to grep for its own content."""
    text = (text or "").strip().lower()
    text = _SLUG_WS_RE.sub("-", text)
    text = _SLUG_STRIP_RE.sub("", text)
    text = text.strip("-")
    return text[:max_len].strip("-") or "concept"


def _slug_id(title: str | None, unique_id: str) -> str:
    return f"{_slugify(title)}-{unique_id[:8]}"


def _parse_markdown(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    frontmatter = yaml.safe_load(m.group(1)) or {}
    return frontmatter, m.group(2)


def _render_markdown(frontmatter: dict[str, Any], body: str) -> str:
    fm_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    body = body.strip("\n")
    if body:
        return f"---\n{fm_text}\n---\n\n{body}\n"
    return f"---\n{fm_text}\n---\n"


def _replace_section(body: str, heading: str, content: str) -> str:
    """Fully regenerates a `## Heading` section from `content` (the section is
    always derived from frontmatter, never hand-appended) - this is what
    keeps repeated write_relations()/write_views() calls idempotent instead
    of accumulating duplicate bullets across incremental runs."""
    lines = body.split("\n")
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if start is None and line.strip() == heading.strip():
            start = i
            continue
        if start is not None and line.startswith("#"):
            end = i
            break
    if start is not None:
        lines = lines[:start] + lines[end:]
    result = "\n".join(lines).strip("\n")
    if content.strip():
        result = f"{result}\n\n{heading}\n\n{content.strip()}" if result else f"{heading}\n\n{content.strip()}"
    return result.strip("\n")


def _relative_link(from_rel_path: str, to_rel_path: str) -> str:
    """Both paths are relative to the project (bundle) root. Returns a
    standard relative markdown link (spec S6.2) from the file at
    `from_rel_path` to the file at `to_rel_path`.

    A leading-slash "bundle-relative absolute" link (spec S6.1) would be
    equally spec-valid, but no generic tool - an IDE, GitHub, a plain file
    browser - treats an OKF project subtree as its root: a
    "/objects/foo.md" link resolves against the filesystem/workspace root
    instead and 404s the moment someone actually clicks it. A genuine
    relative path resolves correctly everywhere, which is what actually
    matters for "browse this with nothing but a file browser."
    """
    from_dir = posixpath.dirname(from_rel_path)
    return posixpath.relpath(to_rel_path, start=from_dir or ".")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bullet(title: str | None, link: str, description: str | None = None) -> str:
    line = f"- [{title or link}]({link})"
    if description:
        line += f" - {description}"
    return line


def _title_for(ko: KnowledgeObject) -> str:
    if ko.type == KnowledgeObjectType.ENTITY:
        return ko.content.get("name") or ko.summary
    return ko.content.get("statement") or ko.summary


def _body_for(ko: KnowledgeObject) -> str:
    if ko.type == KnowledgeObjectType.ENTITY:
        description = ko.content.get("description") or ko.summary
        entity_type = ko.content.get("entity_type") or ko.content.get("type")
        lines = [description]
        if entity_type:
            lines += ["", f"- **유형**: {entity_type}"]
        return "\n".join(lines)
    return ko.content.get("statement") or ko.summary


class OKFWriter:
    def __init__(self, root: Path | str, project_id: str = "default"):
        self._base_root = Path(root)
        self._default_project_id = project_id
        # id -> {path, title, type}, per project - populated as objects/resources
        # are written, and lazily backed by a one-time disk scan on a miss (so a
        # relation/link referencing a concept written by a *prior* OKFWriter
        # instance - e.g. across incremental syncs - still resolves).
        self._object_index: dict[str, dict[str, dict[str, str]]] = {}
        self._resource_index: dict[str, dict[str, dict[str, str]]] = {}
        self._scanned_objects: set[str] = set()
        self._scanned_resources: set[str] = set()
        self._project_root(project_id)

    def _project_root(self, project_id: str) -> Path:
        root = self._base_root / project_id
        # relations/ and links/ are deliberately NOT created here: they only
        # exist when an edge's endpoints fail to resolve (see module
        # docstring), so pre-creating them leaves permanently-empty
        # directories in the common case where every edge inlines cleanly.
        # write_relations()/write_links() create them on demand via
        # _write_markdown()'s mkdir(parents=True).
        for sub in (_RESOURCES_DIR, _OBJECTS_DIR, _VIEWS_DIR):
            (root / sub).mkdir(parents=True, exist_ok=True)
        return root

    def _known_project_ids(self) -> list[str]:
        if not self._base_root.is_dir():
            return [self._default_project_id]
        ids = sorted(d.name for d in self._base_root.iterdir() if d.is_dir())
        return ids or [self._default_project_id]

    def _relpath(self, root: Path, path: Path) -> str:
        return str(path.relative_to(root))

    def _write_markdown(self, root: Path, rel_path: str, frontmatter: dict[str, Any], body: str) -> Path:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_markdown(frontmatter, body), encoding="utf-8")
        return path

    # ---------------------------------------------------------------- index

    def _register_object(self, project_id: str, ko_id: str, rel_path: str, title: str, type_: str) -> None:
        self._object_index.setdefault(project_id, {})[ko_id] = {"path": rel_path, "title": title, "type": type_}

    def _register_resource(
        self, project_id: str, source_id: str, rel_path: str, title: str, content_at: str | None
    ) -> None:
        self._resource_index.setdefault(project_id, {})[source_id] = {
            "path": rel_path,
            "title": title,
            # The Source's own real-world timestamp (modified_at, falling
            # back to collected_at) - write_objects() below reads this back
            # out to stamp the same value on every KnowledgeObject that came
            # from this source, since a KO itself has no timestamp of its
            # own except `extracted_at` (pipeline processing time, not
            # "when this actually happened" - see search/service.py::
            # _content_at's docstring for why that distinction matters).
            "content_at": content_at,
        }

    def _scan_objects(self, root: Path, project_id: str) -> None:
        idx = self._object_index.setdefault(project_id, {})
        objects_dir = root / _OBJECTS_DIR
        if not objects_dir.is_dir():
            return
        for type_dir in objects_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for f in type_dir.glob("*.md"):
                if f.name == "index.md":
                    continue
                fm, _ = _parse_markdown(f.read_text(encoding="utf-8"))
                ko_id = fm.get("knowledge_object_id")
                if ko_id and ko_id not in idx:
                    idx[ko_id] = {"path": self._relpath(root, f), "title": fm.get("title"), "type": type_dir.name}

    def _scan_resources(self, root: Path, project_id: str) -> None:
        idx = self._resource_index.setdefault(project_id, {})
        resources_dir = root / _RESOURCES_DIR
        if not resources_dir.is_dir():
            return
        for f in resources_dir.glob("*.md"):
            if f.name == "index.md":
                continue
            fm, _ = _parse_markdown(f.read_text(encoding="utf-8"))
            source_id = fm.get("source_id")
            if source_id and source_id not in idx:
                generated = fm.get("generated") or {}
                idx[source_id] = {
                    "path": self._relpath(root, f),
                    "title": fm.get("title"),
                    "content_at": fm.get("modified_at") or generated.get("at"),
                }

    def _lookup_object(self, root: Path, project_id: str, ko_id: str) -> dict[str, str] | None:
        idx = self._object_index.setdefault(project_id, {})
        if ko_id not in idx and project_id not in self._scanned_objects:
            self._scan_objects(root, project_id)
            self._scanned_objects.add(project_id)
        return idx.get(ko_id)

    def _lookup_resource(self, root: Path, project_id: str, source_id: str) -> dict[str, str] | None:
        idx = self._resource_index.setdefault(project_id, {})
        if source_id not in idx and project_id not in self._scanned_resources:
            self._scan_resources(root, project_id)
            self._scanned_resources.add(project_id)
        return idx.get(source_id)

    # ------------------------------------------------------------- writing

    def write_resource(self, source: Source, document: Document) -> None:
        project_id = source.project_id
        root = self._project_root(project_id)
        title = document.metadata.title or _slugify(source.uri.rsplit("/", 1)[-1]) or source.source_id[:8]
        rel_path = f"{_RESOURCES_DIR}/{_slug_id(title, source.source_id)}.md"

        frontmatter: dict[str, Any] = {
            "type": _SOURCE_TYPE_TITLES.get(source.source_type.value, "Source"),
            "title": document.metadata.title,
            "resource": source.uri,
            "generated": {"by": f"knowledge_forge/{knowledge_forge.__version__}", "at": _iso(source.collected_at)},
            "source_id": source.source_id,
            "source_type": source.source_type.value,
            "content_hash": source.content_hash,
            "modified_at": _iso(source.modified_at),
            "metadata": source.metadata,
            "document": {"document_id": document.document_id, "element_count": len(document.elements)},
        }
        self._write_markdown(root, rel_path, frontmatter, "## 추출된 개념\n\n(아직 없음)")
        content_at = _iso(source.modified_at) or _iso(source.collected_at)
        self._register_resource(project_id, source.source_id, rel_path, title, content_at)

    def write_objects(self, objects: list[KnowledgeObject]) -> None:
        for ko in objects:
            project_id = ko.source.project_id
            root = self._project_root(project_id)
            title = _title_for(ko)
            rel_path = f"{_OBJECTS_DIR}/{ko.type.value}/{_slug_id(title, ko.knowledge_object_id)}.md"

            src = self._lookup_resource(root, project_id, ko.source.source_id)
            sources = [{"resource": _relative_link(rel_path, src["path"]) if src else ko.source.uri, "title": src["title"] if src else None}]
            # The source's own real-world timestamp, not `ko.extracted_at`
            # (pipeline processing time) - see search/service.py::_content_at.
            # Falls back to extracted_at only if the source's resource file
            # was never found (shouldn't normally happen - write_resource()
            # runs before write_objects() in the pipeline).
            content_at = (src or {}).get("content_at") or _iso(ko.extracted_at)

            frontmatter: dict[str, Any] = {
                "type": ko.type.value.replace("_", " ").title(),
                "title": title,
                "description": ko.summary,
                "resource": ko.source.uri,
                "tags": ko.topics,
                "status": "stable",
                "content_at": content_at,
                "generated": {"by": f"knowledge_forge/{ko.pipeline_version}", "at": _iso(ko.extracted_at)},
                "sources": sources,
                "knowledge_object_id": ko.knowledge_object_id,
                "document": {"document_id": ko.document.document_id, "title": ko.document.title},
                "evidence": [e.model_dump(mode="json") for e in ko.evidence],
                "model_used": ko.model_used,
                "version": ko.version,
                "pipeline_version": ko.pipeline_version,
                "source_hash": ko.source_hash,
                "supersedes": ko.supersedes,
            }
            self._write_markdown(root, rel_path, frontmatter, _body_for(ko))
            self._register_object(project_id, ko.knowledge_object_id, rel_path, title, ko.type.value)

            if ko.supersedes:
                self._deprecate(root, project_id, ko.supersedes, title, rel_path)

    def _deprecate(self, root: Path, project_id: str, old_id: str, new_title: str, new_rel_path: str) -> None:
        old = self._lookup_object(root, project_id, old_id)
        if not old:
            return
        path = root / old["path"]
        fm, body = _parse_markdown(path.read_text(encoding="utf-8"))
        if fm.get("status") == "deprecated":
            return
        fm["status"] = "deprecated"
        note = f"> **Deprecated**: superseded by [{new_title}]({_relative_link(old['path'], new_rel_path)})"
        if not body.lstrip().startswith("> **Deprecated**"):
            body = f"{note}\n\n{body.lstrip()}"
        path.write_text(_render_markdown(fm, body), encoding="utf-8")

    def _append_edge(self, path: Path, key: str, entry: dict[str, Any], heading: str) -> None:
        """Denormalizes one edge endpoint onto `path`'s frontmatter (`key` is
        "relations" or "links") and rebuilds `heading`'s body section from the
        full, now-deduplicated list - see module docstring."""
        text = path.read_text(encoding="utf-8")
        fm, body = _parse_markdown(text)
        entries: list[dict[str, Any]] = fm.setdefault(key, [])
        id_field = "relation_id" if key == "relations" else "link_id"
        if any(e.get(id_field) == entry[id_field] and e.get("direction") == entry["direction"] for e in entries):
            return
        entries.append(entry)
        lines = [
            f"- {e['type']} {'→' if e['direction'] == 'out' else '←'} [{e['peer_title']}]({e['peer']})"
            for e in entries
        ]
        body = _replace_section(body, heading, "\n".join(lines))
        path.write_text(_render_markdown(fm, body), encoding="utf-8")

    def write_relations(self, relations: list[Relation], project_id: str | None = None) -> None:
        project_id = project_id or self._default_project_id
        root = self._project_root(project_id)
        for rel in relations:
            subj = self._lookup_object(root, project_id, rel.subject_id)
            obj = self._lookup_object(root, project_id, rel.object_id)
            if subj and obj:
                self._append_edge(
                    root / subj["path"],
                    "relations",
                    {
                        "relation_id": rel.relation_id,
                        "type": rel.type.value,
                        "direction": "out",
                        "peer": _relative_link(subj["path"], obj["path"]),
                        "peer_title": obj["title"],
                        "label": rel.label,
                        "confidence": rel.confidence,
                    },
                    "## Relations",
                )
                self._append_edge(
                    root / obj["path"],
                    "relations",
                    {
                        "relation_id": rel.relation_id,
                        "type": rel.type.value,
                        "direction": "in",
                        "peer": _relative_link(obj["path"], subj["path"]),
                        "peer_title": subj["title"],
                        "label": rel.label,
                        "confidence": rel.confidence,
                    },
                    "## Relations",
                )
            else:
                payload = rel.model_dump(mode="json")
                payload["relation_type"] = payload.pop("type")
                frontmatter = {"type": "Relation", **payload}
                self._write_markdown(
                    root,
                    f"{_RELATIONS_DIR}/{rel.relation_id}.md",
                    frontmatter,
                    "_대상 concept를 찾을 수 없어 엣지를 인라인화하지 못했습니다._",
                )

    def write_links(self, links: list[Link], project_id: str | None = None) -> None:
        project_id = project_id or self._default_project_id
        root = self._project_root(project_id)
        for link in links:
            src = self._lookup_resource(root, project_id, link.source_ref.source_id)
            tgt = self._lookup_resource(root, project_id, link.target_ref.source_id)
            if src and tgt:
                self._append_edge(
                    root / src["path"],
                    "links",
                    {
                        "link_id": link.link_id,
                        "type": link.type.value,
                        "direction": "out",
                        "peer": _relative_link(src["path"], tgt["path"]),
                        "peer_title": tgt["title"],
                        "label": link.label,
                        "confidence": link.confidence,
                    },
                    "## Links",
                )
                self._append_edge(
                    root / tgt["path"],
                    "links",
                    {
                        "link_id": link.link_id,
                        "type": link.type.value,
                        "direction": "in",
                        "peer": _relative_link(tgt["path"], src["path"]),
                        "peer_title": src["title"],
                        "label": link.label,
                        "confidence": link.confidence,
                    },
                    "## Links",
                )
            else:
                payload = link.model_dump(mode="json")
                payload["link_type"] = payload.pop("type")
                frontmatter = {"type": "Link", **payload}
                self._write_markdown(
                    root,
                    f"{_LINKS_DIR}/{link.link_id}.md",
                    frontmatter,
                    "_대상 concept를 찾을 수 없어 엣지를 인라인화하지 못했습니다._",
                )

    # ------------------------------------------------------- manifest/views

    def write_manifest(self) -> dict[str, Path]:
        return {project_id: self._write_manifest_for(project_id) for project_id in self._known_project_ids()}

    def write_views(self) -> dict[str, list[Path]]:
        return {project_id: self._write_views_for(project_id) for project_id in self._known_project_ids()}

    def _write_manifest_for(self, project_id: str) -> Path:
        root = self._project_root(project_id)
        manifest = {
            "generated_at": _now_iso(),
            "pipeline_version": knowledge_forge.__version__,
            "resources": self._catalog_resources(root),
            "objects": self._catalog_objects(root),
            "relations": self._catalog_orphans(root, _RELATIONS_DIR),
            "links": self._catalog_orphans(root, _LINKS_DIR),
        }
        path = root / "manifest.yaml"
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False)
        return path

    def _catalog_resources(self, root: Path) -> list[dict[str, Any]]:
        entries = []
        for f in sorted((root / _RESOURCES_DIR).glob("*.md")):
            if f.name == "index.md":
                continue
            fm, _ = _parse_markdown(f.read_text(encoding="utf-8"))
            entries.append(
                {"id": fm.get("source_id", f.stem), "type": fm.get("source_type", "unknown"), "path": self._relpath(root, f)}
            )
        return entries

    def _catalog_objects(self, root: Path) -> list[dict[str, Any]]:
        entries = []
        objects_dir = root / _OBJECTS_DIR
        for type_dir in sorted(d for d in objects_dir.iterdir() if d.is_dir()):
            for f in sorted(type_dir.glob("*.md")):
                if f.name == "index.md":
                    continue
                fm, _ = _parse_markdown(f.read_text(encoding="utf-8"))
                entries.append({"id": fm.get("knowledge_object_id", f.stem), "type": type_dir.name, "path": self._relpath(root, f)})
        return entries

    def _catalog_orphans(self, root: Path, dirname: str) -> list[dict[str, Any]]:
        entries = []
        for f in sorted((root / dirname).glob("*.md")):
            fm, _ = _parse_markdown(f.read_text(encoding="utf-8"))
            id_field = "relation_id" if dirname == _RELATIONS_DIR else "link_id"
            entries.append({"id": fm.get(id_field, f.stem), "type": fm.get("type", "unknown"), "path": self._relpath(root, f)})
        return entries

    def _write_views_for(self, project_id: str) -> list[Path]:
        root = self._project_root(project_id)
        objects_dir = root / _OBJECTS_DIR

        by_topic: dict[str, list[dict[str, Any]]] = {}
        by_document: dict[str, list[dict[str, Any]]] = {}
        by_type: dict[str, list[dict[str, Any]]] = {}
        timeline: list[dict[str, Any]] = []

        for type_dir in sorted(d for d in objects_dir.iterdir() if d.is_dir()):
            for f in sorted(type_dir.glob("*.md")):
                if f.name == "index.md":
                    continue
                fm, _ = _parse_markdown(f.read_text(encoding="utf-8"))
                entry = {
                    "id": fm.get("knowledge_object_id", f.stem),
                    "title": fm.get("title"),
                    "description": fm.get("description"),
                    "path": self._relpath(root, f),
                }
                by_type.setdefault(type_dir.name, []).append(entry)
                for topic in fm.get("tags") or []:
                    by_topic.setdefault(topic, []).append(entry)
                document_id = (fm.get("document") or {}).get("document_id")
                if document_id:
                    by_document.setdefault(document_id, []).append(entry)
                generated_at = (fm.get("generated") or {}).get("at")
                if generated_at:
                    timeline.append({**entry, "at": generated_at})

        timeline.sort(key=lambda e: e["at"])

        written = [self._write_by_topic(root, by_topic), self._write_log(root, timeline)]
        self._rebuild_resource_bodies(root, by_document)
        written += self._write_indexes(root, by_type)
        return written

    def _write_by_topic(self, root: Path, by_topic: dict[str, list[dict[str, Any]]]) -> Path:
        from_path = f"{_VIEWS_DIR}/by-topic.md"
        lines = []
        for topic in sorted(by_topic):
            lines.append(f"## {topic}")
            lines.append("")
            for e in sorted(by_topic[topic], key=lambda e: e["title"] or ""):
                lines.append(_bullet(e["title"], _relative_link(from_path, e["path"]), e.get("description")))
            lines.append("")
        frontmatter = {"type": "Topic Index", "generated": {"by": f"knowledge_forge/{knowledge_forge.__version__}", "at": _now_iso()}}
        return self._write_markdown(root, f"{_VIEWS_DIR}/by-topic.md", frontmatter, "\n".join(lines))

    def _write_log(self, root: Path, timeline: list[dict[str, Any]]) -> Path:
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in timeline:
            by_date[e["at"][:10]].append(e)
        lines = ["# Directory Update Log"]
        for date in sorted(by_date, reverse=True):
            lines.append("")
            lines.append(f"## {date}")
            lines.append("")
            for e in by_date[date]:
                link = _relative_link("log.md", e["path"])
                lines.append(f"* **Extraction**: {_bullet(e['title'], link, e.get('description'))[2:]}")
        path = root / "log.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _rebuild_resource_bodies(self, root: Path, by_document: dict[str, list[dict[str, Any]]]) -> None:
        for f in sorted((root / _RESOURCES_DIR).glob("*.md")):
            if f.name == "index.md":
                continue
            fm, body = _parse_markdown(f.read_text(encoding="utf-8"))
            document_id = (fm.get("document") or {}).get("document_id")
            entries = by_document.get(document_id, []) if document_id else []
            resource_rel = self._relpath(root, f)
            lines = [_bullet(e["title"], _relative_link(resource_rel, e["path"]), e.get("description")) for e in entries] or [
                "(아직 없음)"
            ]
            body = _replace_section(body, "## 추출된 개념", "\n".join(lines))
            f.write_text(_render_markdown(fm, body), encoding="utf-8")

    def _write_indexes(self, root: Path, by_type: dict[str, list[dict[str, Any]]]) -> list[Path]:
        written = []

        lines = ["# Objects", ""]
        for type_name in sorted(by_type):
            lines.append(f"* [{type_name}](objects/{type_name}/index.md) - {len(by_type[type_name])}개")
        lines += ["", "# Resources", "", "* [resources](resources/index.md)", "", "# Views", ""]
        lines.append("* [by-topic](views/by-topic.md)")
        lines.append("* [log](log.md)")
        fm_text = yaml.safe_dump({"okf_version": _OKF_VERSION}, sort_keys=False).strip()
        root_index = root / "index.md"
        root_index.write_text(f"---\n{fm_text}\n---\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        written.append(root_index)

        for type_name, entries in sorted(by_type.items()):
            idx_lines = [f"# {type_name}", ""]
            for e in sorted(entries, key=lambda e: e["title"] or ""):
                idx_lines.append(_bullet(e["title"], Path(e["path"]).name, e.get("description")))
            idx_path = root / _OBJECTS_DIR / type_name / "index.md"
            idx_path.write_text("\n".join(idx_lines) + "\n", encoding="utf-8")
            written.append(idx_path)

        resources_lines = ["# Resources", ""]
        for f in sorted((root / _RESOURCES_DIR).glob("*.md")):
            if f.name == "index.md":
                continue
            fm, _ = _parse_markdown(f.read_text(encoding="utf-8"))
            resources_lines.append(f"* [{fm.get('title') or f.stem}]({f.name}) - {fm.get('type', '')}")
        res_index = root / _RESOURCES_DIR / "index.md"
        res_index.write_text("\n".join(resources_lines) + "\n", encoding="utf-8")
        written.append(res_index)

        return written
