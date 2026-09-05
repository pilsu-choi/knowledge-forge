"""Check a golden file against a live index: every answer document/chunk/
knowledge_object id must exist, chunk/KO ids must belong to the answer
documents, `source_hash` must still reproduce from sources.content_hash
(`check_staleness`), and - when the file carries a dev/test split - the two
halves must actually be labelled and comparable. Run by hand (needs a DB -
`KF_TEST_DATABASE_URL` or the dev DB DSN), not by pytest; the pure schema
checks live in tests/evaluation/test_golden.py and need no DB.

    uv run python tests/eval/golden/validate_golden.py [path/to/golden.json]

기본값이 v1.json인 이유: v0.json은 과거 run의 기준선이라 더 이상 바뀌지
않고, 실제로 드리프트를 감시해야 하는 건 현행 세트다. v0을 검사하려면
경로를 명시적으로 넘긴다.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import psycopg

from knowledge_forge.evaluation.golden import check_staleness, load_golden

DEFAULT_DSN = "postgresql://kf:kf@localhost:15432/knowledge_forge"


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path(__file__).with_name("v1.json")
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
    warnings: list[str] = []
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
        # count 질의는 정답 문서 목록과 개수가 어긋나면 둘 중 하나가 틀린 것이다.
        if q.expected.count is not None and q.expected.document_ids:
            if q.expected.count != len(q.expected.document_ids):
                problems.append(
                    f"{q.id}: count={q.expected.count} but {len(q.expected.document_ids)} answer documents"
                )
        if not q.notes.strip():
            # 경고이지 무결성 오류가 아니다 - v0.json은 빈 notes 5건을 가진 채
            # 얼어붙었고, 이 스크립트는 v0을 검사할 때도 exit 0이어야 한다.
            # "새 세트는 notes가 있어야 한다"는 규칙은 DB가 필요 없으므로
            # tests/evaluation/test_golden.py::test_v1_invariants_hold_per_query가 강제한다.
            warnings.append(f"{q.id}: notes is empty (a human auditor cannot check the answer)")
    for issue in check_staleness(golden, hash_by_doc):
        problems.append(f"{issue.query_id}: {issue.reason} {list(issue.document_ids)}")

    n_with_docs = sum(1 for q in golden.queries if q.expected.document_ids)
    n_none = sum(1 for q in golden.queries if q.expected.none)
    print(
        f"{path.name}: golden_version={golden.golden_version} queries={len(golden.queries)} "
        f"(with documents={n_with_docs}, none={n_none}) index documents={n_docs} "
        f"(snapshot {golden.index_snapshot.documents})"
    )
    print("origin: " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(q.origin for q in golden.queries).items())))
    print("by primary_type: " + ", ".join(f"{t}={n}" for t, n in golden.count_by_type(primary_only=True).items()))
    if golden.has_split():
        print("split: " + ", ".join(f"{k}={v}" for k, v in golden.count_by_split().items()))
        dev = golden.count_by_type(primary_only=True, split="dev")
        test = golden.count_by_type(primary_only=True, split="test")
        print(f"{'primary_type':<28}{'dev':>5}{'test':>6}")
        for t in sorted(set(dev) | set(test)):
            d, s = dev.get(t, 0), test.get(t, 0)
            print(f"{t:<28}{d:>5}{s:>6}" + ("   <- unbalanced" if abs(d - s) > 1 else ""))
    if warnings:
        print(f"{len(warnings)} warning(s):")
        print("\n".join(f"  {w}" for w in warnings))
    if problems:
        print(f"{len(problems)} problem(s):")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    print("OK: all document/chunk/knowledge_object ids exist and source_hash matches the index")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
