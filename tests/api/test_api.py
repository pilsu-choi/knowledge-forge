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
from knowledge_forge.models.knowledge_object import DocumentRef, KnowledgeObject, KnowledgeObjectType
from knowledge_forge.models.relation import Relation, RelationType
from knowledge_forge.models.source import SourceRef, SourceType
from knowledge_forge.storage.job_store import Job, JobStatus


class _FakeJobStore:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, job_type: str, payload: dict) -> Job:
        self.enqueued.append((job_type, payload))
        job = Job(
            job_id="job-1",
            job_type=job_type,
            payload=payload,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )
        self.jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def list(self, status=None, limit=50, offset=0) -> list[Job]:
        jobs = list(self.jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
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


class _FakeReadStore:
    def __init__(self):
        self.list_kwargs = None

    async def list_knowledge_objects(self, source_id=None, type=None, limit=50, offset=0):
        self.list_kwargs = {"source_id": source_id, "type": type, "limit": limit, "offset": offset}
        return [_sample_ko()]

    async def get_knowledge_object(self, knowledge_object_id: str):
        return _sample_ko() if knowledge_object_id == "ko-1" else None

    async def list_relations(self, subject_id=None, object_id=None, type=None, limit=50, offset=0):
        return [_sample_relation()]


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


def test_ingest_missing_path_returns_400():
    client = _make_client(_FakeJobStore(), _FakeReadStore())

    resp = client.post("/ingest", json={"path": "/no/such/path"})

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

    resp = client.get("/knowledge-objects", params={"source_id": "src-1", "type": "fact", "limit": 10, "offset": 5})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["knowledge_object_id"] == "ko-1"
    assert read_store.list_kwargs == {"source_id": "src-1", "type": "fact", "limit": 10, "offset": 5}


def test_get_knowledge_object_404():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    resp = client.get("/knowledge-objects/nope")
    assert resp.status_code == 404


def test_list_relations():
    client = _make_client(_FakeJobStore(), _FakeReadStore())
    resp = client.get("/relations", params={"subject_id": "ko-1"})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["relation_id"] == "rel-1"
