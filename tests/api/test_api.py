"""API layer tests: FastAPI TestClient + dependency_overrides against
in-memory fakes - no real Postgres needed (that's covered by
tests/storage/test_job_store.py, which is a real KF_TEST_DATABASE_URL
integration test). Fakes implement just the subset of
JobStore/PostgresStore's interface the routes call.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from knowledge_forge.api.app import create_app
from knowledge_forge.api.deps import get_job_store, get_read_store
from knowledge_forge.config import settings
from knowledge_forge.models.document import Document, DocumentMetadata, TextBlock
from knowledge_forge.models.knowledge_object import DocumentRef, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import Source, SourceRef, SourceType
from knowledge_forge.storage.job_store import Job, JobStatus


class _FakeJobStore:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.enqueued: list[tuple[str, dict, str]] = []

    async def enqueue(self, job_type: str, payload: dict, project_id: str = "default") -> Job:
        self.enqueued.append((job_type, payload, project_id))
        job = Job(
            job_id="job-1",
            job_type=job_type,
            payload=payload,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            project_id=project_id,
        )
        self.jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def list(self, status=None, project_id=None, limit=50, offset=0) -> list[Job]:
        jobs = list(self.jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        if project_id is not None:
            jobs = [j for j in jobs if j.project_id == project_id]
        return jobs[offset : offset + limit]


def _sample_ko() -> KnowledgeObject:
    return KnowledgeObject(
        knowledge_object_id="ko-1",
        type=KnowledgeObjectType.FACT,
        source=SourceRef(source_id="src-1", source_type=SourceType.LOCAL_FILE, uri="file:///a.md"),
        document=DocumentRef(document_id="doc-1"),
        summary="a fact",
        content={"statement": "hello"},
        topics=["t1"],
        document_type="text",
        extracted_at=datetime.now(timezone.utc),
        model_used="test-model",
        pipeline_version="v1",
        source_hash="h1",
    )


def _sample_relation() -> Relation:
    return Relation(
        relation_id="rel-1",
        type=RelationType.MENTIONS,
        subject_id="ko-1",
        object_id="ko-2",
        created_at=datetime.now(timezone.utc),
    )


def _sample_source() -> Source:
    return Source(
        source_id="src-1",
        source_type=SourceType.LOCAL_FILE,
        uri="file:///a.md",
        content_hash="h1",
        collected_at=datetime.now(timezone.utc),
        project_id="proj-a",
    )


def _sample_document() -> Document:
    return Document(
        document_id="doc-1",
        source=SourceRef.from_source(_sample_source()),
        elements=[TextBlock(element_id="e1", text="hello")],
        metadata=DocumentMetadata(title="T"),
    )


class _FakeReadStore:
    def __init__(self):
        self.list_kwargs = None
        self.list_sources_kwargs = None
        self.list_documents_kwargs = None

    async def list_knowledge_objects(self, source_id=None, type=None, limit=50, offset=0, *, project_id=None):
        self.list_kwargs = {"source_id": source_id, "type": type, "limit": limit, "offset": offset, "project_id": project_id}
        return [_sample_ko()]

    async def get_knowledge_object(self, knowledge_object_id: str, *, project_id=None):
        ko = _sample_ko()
        if knowledge_object_id != "ko-1" or project_id not in (None, ko.source.project_id):
            return None
        return ko

    async def neighbors(self, object_id, **kwargs):
        self.neighbors_kwargs = kwargs
        start = await self.get_knowledge_object(object_id, project_id=kwargs["project_id"])
        return None if start is None else (start, [])

    async def list_relations(self, subject_id=None, object_id=None, type=None, limit=50, offset=0):
        return [_sample_relation()]

    async def list_sources(self, project_id=None, limit=50, offset=0):
        self.list_sources_kwargs = {"project_id": project_id, "limit": limit, "offset": offset}
        return [_sample_source()]

    async def get_source(self, source_id: str):
        return _sample_source() if source_id == "src-1" else None

    async def list_documents(self, project_id=None, source_id=None, limit=50, offset=0):
        self.list_documents_kwargs = {"project_id": project_id, "source_id": source_id, "limit": limit, "offset": offset}
        return [_sample_document()]

    async def get_document(self, document_id: str):
        return _sample_document() if document_id == "doc-1" else None

    async def list_chunks_by_document(self, document_id: str, *, hierarchy_level=None):
        from knowledge_forge.models.chunk import Chunk, HierarchyLevel

        return [
            Chunk(
                chunk_id=f"chunk-{i}",
                document_id=document_id,
                source_id="src-1",
                hierarchy_level=HierarchyLevel.PARAGRAPH,
                sequence_index=i,
                text=f"paragraph {i}",
                token_count=2,
                page_range=(i + 1, i + 1),
            )
            for i in range(3)
        ]


def _make_client(job_store: _FakeJobStore, read_store: _FakeReadStore) -> TestClient:
    app = create_app()

    async def _job_store_override():
        return job_store

    async def _read_store_override():
        return read_store

    app.dependency_overrides[get_job_store] = _job_store_override
    app.dependency_overrides[get_read_store] = _read_store_override
    return TestClient(app)


def test_health_needs_no_db():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ingest_existing_path_returns_202_with_job_id(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    client = _make_client(_FakeJobStore(), _FakeReadStore())

    resp = client.post("/ingest", json={"path": str(tmp_path / "a.md")})

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] == "job-1"
    assert body["status"] == "pending"


def test_ingest_forwards_project_id_to_job_payload(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    resp = client.post("/ingest", json={"path": str(tmp_path / "a.md"), "project_id": "proj-a"})

    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-1"
    assert job_store.enqueued[0][2] == "proj-a"


def test_ingest_defaults_project_id(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    client.post("/ingest", json={"path": str(tmp_path / "a.md")})

    assert job_store.enqueued[0][2] == "default"


def test_ingest_missing_path_returns_400():
    client = _make_client(_FakeJobStore(), _FakeReadStore())

    resp = client.post("/ingest", json={"path": "/no/such/path"})

    assert resp.status_code == 400


def test_ingest_local_without_path_returns_400():
    client = _make_client(_FakeJobStore(), _FakeReadStore())

    resp = client.post("/ingest", json={})

    assert resp.status_code == 400


def test_ingest_saas_source_enqueues_matching_job_type(monkeypatch):
    monkeypatch.setattr(
        "knowledge_forge.api.routes.ingest.SAAS_COLLECTOR_BUILDERS", {"slack": lambda: None}
    )
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    resp = client.post("/ingest", json={"source": "slack", "project_id": "PRJ-A"})

    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-1"
    job_type, payload, project_id = job_store.enqueued[0]
    assert job_type == "slack_ingest"
    assert project_id == "PRJ-A"
    assert "path" not in payload


def test_ingest_forwards_okf_dir_to_job_payload(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    resp = client.post("/ingest", json={"path": str(tmp_path / "a.md"), "okf_dir": "/tmp/okf"})

    assert resp.status_code == 202
    assert job_store.enqueued[0][1]["okf_dir"] == "/tmp/okf"


def test_ingest_omits_okf_dir_defaults_to_none(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    client.post("/ingest", json={"path": str(tmp_path / "a.md")})

    assert job_store.enqueued[0][1]["okf_dir"] is None


def test_ingest_with_since_becomes_a_backfill_job(monkeypatch):
    """since/until (see wiki/scheduling.md "놓친 구간 재수집") round-trip through
    the job payload as ISO strings - worker.py's _dispatch parses them back."""
    monkeypatch.setattr(
        "knowledge_forge.api.routes.ingest.SAAS_COLLECTOR_BUILDERS", {"slack": lambda: None}
    )
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    resp = client.post(
        "/ingest",
        json={
            "source": "slack",
            "project_id": "PRJ-A",
            "since": "2026-08-01T09:00:00",
            "until": "2026-08-01T18:00:00",
        },
    )

    assert resp.status_code == 202
    payload = job_store.enqueued[0][1]
    assert payload["since"] == "2026-08-01T09:00:00+00:00"  # naive input normalized to UTC
    assert payload["until"] == "2026-08-01T18:00:00+00:00"


def test_ingest_without_since_omits_backfill_fields_from_payload(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    client.post("/ingest", json={"path": str(tmp_path / "a.md")})

    payload = job_store.enqueued[0][1]
    assert "since" not in payload
    assert "until" not in payload


def test_ingest_since_after_until_returns_400(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    client = _make_client(_FakeJobStore(), _FakeReadStore())

    resp = client.post(
        "/ingest",
        json={
            "path": str(tmp_path / "a.md"),
            "since": "2026-08-01T18:00:00Z",
            "until": "2026-08-01T09:00:00Z",
        },
    )

    assert resp.status_code == 400


def test_ingest_unknown_source_returns_400(monkeypatch):
    monkeypatch.setattr(
        "knowledge_forge.api.routes.ingest.SAAS_COLLECTOR_BUILDERS", {"slack": lambda: None}
    )
    client = _make_client(_FakeJobStore(), _FakeReadStore())

    resp = client.post("/ingest", json={"source": "carrier-pigeon"})

    assert resp.status_code == 400


def test_get_job_roundtrip_and_404(tmp_path):
    (tmp_path / "a.md").write_text("hello")
    job_store = _FakeJobStore()
    client = _make_client(job_store, _FakeReadStore())

    created = client.post("/ingest", json={"path": str(tmp_path / "a.md")}).json()
    resp = client.get(f"/jobs/{created['job_id']}")
    assert resp.status_code == 200
    assert resp.json()["job_id"] == created["job_id"]

    missing = client.get("/jobs/does-not-exist")
    assert missing.status_code == 404


def test_list_knowledge_objects_forwards_filters():
    read_store = _FakeReadStore()
    client = _make_client(_FakeJobStore(), read_store)

    resp = client.get(
        "/knowledge-objects", params={"source_id": "src-1", "type": "fact", "limit": 10, "offset": 5, "project_id": "p"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["knowledge_object_id"] == "ko-1"
    assert read_store.list_kwargs == {"source_id": "src-1", "type": "fact", "limit": 10, "offset": 5, "project_id": "p"}


def test_get_knowledge_object_outside_project_is_404():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    assert client.get("/knowledge-objects/ko-1", params={"project_id": "other"}).status_code == 404


def test_expand_relations_clamps_depth_validates_types_and_404s():
    read_store = _FakeReadStore()
    client = _make_client(_FakeJobStore(), read_store)

    resp = client.get("/relations/expand", params={"object_id": "ko-1", "depth": 9, "relation_types": ["caused_by"]})
    assert resp.status_code == 200
    assert resp.json()["object"]["knowledge_object_id"] == "ko-1"
    assert read_store.neighbors_kwargs["depth"] == 3
    assert read_store.neighbors_kwargs["relation_types"] == ["caused_by"]

    assert client.get("/relations/expand", params={"object_id": "ko-1", "relation_types": ["nope"]}).status_code == 400
    assert client.get("/relations/expand", params={"object_id": "missing"}).status_code == 404


def test_get_knowledge_object_404():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    resp = client.get("/knowledge-objects/nope")
    assert resp.status_code == 404


def test_list_relations():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    resp = client.get("/relations", params={"subject_id": "ko-1"})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["relation_id"] == "rel-1"


def test_list_sources_forwards_project_id_filter():
    read_store = _FakeReadStore()
    client = _make_client(_FakeJobStore(), read_store)

    resp = client.get("/sources", params={"project_id": "proj-a", "limit": 10, "offset": 5})

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["source_id"] == "src-1"
    assert body["items"][0]["project_id"] == "proj-a"
    assert read_store.list_sources_kwargs == {"project_id": "proj-a", "limit": 10, "offset": 5}


def test_get_source_404():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    resp = client.get("/sources/nope")
    assert resp.status_code == 404


def test_list_documents_forwards_filters():
    read_store = _FakeReadStore()
    client = _make_client(_FakeJobStore(), read_store)

    resp = client.get("/documents", params={"project_id": "proj-a", "source_id": "src-1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["document_id"] == "doc-1"
    assert body["items"][0]["project_id"] == "proj-a"
    assert read_store.list_documents_kwargs == {
        "project_id": "proj-a",
        "source_id": "src-1",
        "limit": 50,
        "offset": 0,
    }


def test_get_document_404():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    resp = client.get("/documents/nope")
    assert resp.status_code == 404


def test_get_document_chunks_returns_ordered_chunks_and_pagination_fields():
    client = _make_client(_FakeJobStore(), _FakeReadStore())

    resp = client.get("/documents/doc-1/chunks", params={"max_chars": 25})

    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == "doc-1"
    assert body["title"] == "T"
    assert body["source"]["source_id"] == "src-1"
    assert body["source"]["source_type"] == "local_file"
    assert body["total_chunks"] == 3
    assert body["total_tokens"] == 6
    assert [c["sequence_index"] for c in body["chunks"]] == [0, 1]
    assert body["chunks"][0] == {"chunk_id": "chunk-0", "sequence_index": 0, "page_start": 1, "page_end": 1, "text": "paragraph 0"}
    assert body["truncated"] is True
    assert body["next_offset"] == 2

    resp = client.get("/documents/doc-1/chunks", params={"offset": body["next_offset"]})
    assert resp.status_code == 200
    assert [c["sequence_index"] for c in resp.json()["chunks"]] == [2]
    assert resp.json()["truncated"] is False


def test_get_document_chunks_404_and_422():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    assert client.get("/documents/nope/chunks").status_code == 404
    assert client.get("/documents/doc-1/chunks", params={"max_chars": 0}).status_code == 422


class _FakeLifespanJobStore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def init_schema(self):
        return None


def test_lifespan_starts_auto_enqueue_scheduler_when_configured(monkeypatch):
    """`serve` starting is enough to also start periodic sync when
    KF_SCHEDULED_SOURCES is set - the whole point of auto_enqueue.py."""
    calls = []

    class _FakeScheduler:
        def shutdown(self):
            calls.append("shutdown")

    def fake_start(dsn, sources):
        calls.append(("start", dsn, sources))
        return _FakeScheduler()

    monkeypatch.setattr("knowledge_forge.api.app.JobStore", lambda dsn: _FakeLifespanJobStore())
    monkeypatch.setattr("knowledge_forge.api.app.start_auto_enqueue_scheduler", fake_start)
    monkeypatch.setattr(settings, "database_url", "postgresql://fake")
    monkeypatch.setattr(settings, "scheduled_sources", '[{"source": "local", "path": "/app/input"}]')

    with TestClient(create_app()):
        pass

    assert calls[0][0] == "start"
    assert calls[0][1] == "postgresql://fake"
    assert calls[0][2][0].path == "/app/input"
    assert calls[-1] == "shutdown"


def test_lifespan_skips_scheduler_when_not_configured(monkeypatch):
    started = []
    monkeypatch.setattr("knowledge_forge.api.app.JobStore", lambda dsn: _FakeLifespanJobStore())
    monkeypatch.setattr(
        "knowledge_forge.api.app.start_auto_enqueue_scheduler",
        lambda dsn, sources: started.append(sources) or None,
    )
    monkeypatch.setattr(settings, "database_url", "postgresql://fake")
    monkeypatch.setattr(settings, "scheduled_sources", None)

    with TestClient(create_app()):
        pass

    assert started == []
