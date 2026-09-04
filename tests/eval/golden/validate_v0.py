"""Check a golden file against a live index: every answer document/chunk/
knowledge_object id must exist, chunk/KO ids must belong to the answer
documents, and `source_hash` must still reproduce from sources.content_hash
(`check_staleness`). Run by hand (needs a DB - `KF_TEST_DATABASE_URL` or
the dev DB DSN), not by pytest; the pure schema checks live in
tests/evaluation/test_golden.py and need no DB.

    uv run python tests/eval/golden/validate_v0.py [path/to/golden.json]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

from knowledge_forge.evaluation.golden import check_staleness, load_golden

DEFAULT_DSN = "postgresql://kf:kf@localhost:15432/knowledge_forge"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).with_name("v0.json")
    dsn = os.environ.get("KF_TEST_DATABASE_URL") or os.environ.get("KF_DATABASE_URL") or DEFAULT_DSN
    golden = load_golden(path)

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("select d.document_id, s.content_hash from documents d join sources s using(source_id)")
        hash_by_doc: dict[str, str] = dict(cur.fetchall())
        cur.execute("select chunk_id, document_id from chunks")
        doc_by_chunk: dict[str, str] = dict(cur.fetchall())
        cur.execute("select knowledge_object_id, document_id from knowledge_objects")
        doc_by_ko: dict[str, str] = dict(cur.fetchall())
        cur.execute("select count(*) from documents")
        n_docs = cur.fetchone()[0]

    problems: list[str] = []
    for q in golden.queries:
        for d in q.expected.document_ids:
            if d not in hash_by_doc:
                problems.append(f"{q.id}: document {d} not in index")
        for c in q.expected.chunk_ids:
            if c not in doc_by_chunk:
                problems.append(f"{q.id}: chunk {c} not in index")
            elif doc_by_chunk[c] not in q.expected.document_ids:
                problems.append(f"{q.id}: chunk {c} belongs to {doc_by_chunk[c]}, not an answer document")
        for k in q.expected.knowledge_object_ids:
            if k not in doc_by_ko:
                problems.append(f"{q.id}: knowledge_object {k} not in index")
            elif doc_by_ko[k] not in q.expected.document_ids:
                problems.append(f"{q.id}: knowledge_object {k} belongs to {doc_by_ko[k]}, not an answer document")
    for issue in check_staleness(golden, hash_by_doc):
        problems.append(f"{issue.query_id}: {issue.reason} {list(issue.document_ids)}")

    n_with_docs = sum(1 for q in golden.queries if q.expected.document_ids)
    n_none = sum(1 for q in golden.queries if q.expected.none)
    print(
        f"{path.name}: golden_version={golden.golden_version} queries={len(golden.queries)} "
        f"(with documents={n_with_docs}, none={n_none}) index documents={n_docs} "
        f"(snapshot {golden.index_snapshot.documents})"
    )
    print("by primary_type: " + ", ".join(f"{t}={n}" for t, n in golden.count_by_type(primary_only=True).items()))
    if problems:
        print(f"{len(problems)} problem(s):")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    print("OK: all document/chunk/knowledge_object ids exist and source_hash matches the index")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
