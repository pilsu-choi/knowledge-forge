"""Text-to-SQL search (wiki/search-architecture.md's "⑥ Temporal Search -
Text-to-SQL/Temporal Join" leg). Unlike `file_search`/`vector_search`, this
tool has no query-understanding layer of its own at all - the calling model
already IS the LLM that would otherwise translate a natural-language
question into SQL, so `agent/tools.py` just hands it the schema (this
module's `SCHEMA_DESCRIPTION`) and lets it write the SQL directly as the
tool argument. A nested "text-to-SQL" LLM call (the way `search/planner.py`
needs one for decomposition) would just duplicate reasoning the top-level
model is already doing in this turn.

This is also the general answer to two gaps `wiki/search-architecture.md`
flags as unimplemented elsewhere: attribute/entity filtering ("항목을
언급한 사람이 유지희인 것만") and temporal joins across KnowledgeObject
types ("지난달 결정된 것 중 미완료인 것") - both are just `WHERE`/`JOIN`
clauses once the model can write SQL directly against `knowledge_objects`/
`relations`/`sources`, instead of needing a bespoke filter parameter built
for every attribute someone might ask about.

Safety is three independent layers (defense in depth - a well-behaved model
only ever needs the first, but retrieved document content or an adversarial
prompt could otherwise talk a model into writing something it shouldn't):

1. `_validate_readonly_sql()` here - a fast, clear-error string check
   *before* anything reaches Postgres: single statement, starts with
   SELECT/WITH, no write/DDL-shaped keyword anywhere.
2. `PostgresStore.execute_readonly_query()` runs it inside a
   `SET TRANSACTION READ ONLY` transaction - Postgres itself rejects any
   write statement regardless of what (1) missed.
3. That same transaction is `force_rollback=True` - unconditionally rolled
   back even on success, so nothing persists even in some edge case neither
   (1) nor (2) anticipated.

Row limits are enforced server-side (`PostgresStore.execute_readonly_query`
wraps the query as a subquery under its own `LIMIT`) rather than trusting
whatever `LIMIT` the model wrote or forgot to write.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from knowledge_forge.storage.store import PipelineStore

DEFAULT_MAX_ROWS = 200
# A plain list of numbers this long is almost certainly an embedding vector
# (1536 dims for the default model, embeddings.md) rather than real result
# data - selecting it whole would otherwise blow up the tool result's token
# cost for no benefit to the caller.
_EMBEDDING_LIST_THRESHOLD = 50

_LEADING_KEYWORD_RE = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_KEYWORD_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|COPY|CALL|"
    r"VACUUM|EXECUTE|MERGE|REINDEX|LISTEN|NOTIFY|LOCK|SECURITY)\b",
    re.IGNORECASE,
)

SCHEMA_DESCRIPTION = """사용 가능한 테이블(PostgreSQL, 읽기 전용):

sources(source_id TEXT PK, source_type TEXT, uri TEXT, collected_at TIMESTAMPTZ,
        modified_at TIMESTAMPTZ, project_id TEXT, metadata JSONB)
  원본 문서/메시지 1건. source_type: local_file/slack/gmail/google_drive/notion.

documents(document_id TEXT PK, source_id TEXT REFERENCES sources, title TEXT NULL, metadata JSONB,
          project_id TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)
  파싱된 문서 1건(source와 1:1). title은 파일명/메일 제목/Slack "#채널 날짜"/첫 제목에서 파생된
  검색용 제목이다(없으면 NULL) - 제목으로 문서를 찾을 땐 document_search 도구가 더 적합하지만,
  SQL에서는 title ILIKE '%...%'로 조인 조건에 쓸 수 있다. elements(JSONB, 문서 요소 전체)
  컬럼도 있지만 절대 SELECT하지 말 것(문서 하나가 수십 KB) - 필요하면 chunks.text를 쓸 것.

chunks(chunk_id TEXT PK, document_id TEXT REFERENCES documents, source_id TEXT REFERENCES sources,
       sequence_index INT, text TEXT, token_count INT, page_start INT, page_end INT)
  문서를 나눈 원문 청크(document 안에서 sequence_index 순). text는 최대 수천 토큰이라 전체를
  SELECT하지 말고 LEFT(text, 300) 같이 잘라서 볼 것 - 본문 문자열 일치 검색은 lexical_search
  도구가 더 적합하다. project_id 컬럼이 없다 - documents 또는 sources와 JOIN해서 얻을 것.

knowledge_objects(knowledge_object_id TEXT PK, type TEXT, source_id TEXT REFERENCES sources,
                   document_id TEXT, summary TEXT, content JSONB, topics JSONB(문자열 배열),
                   document_type TEXT, extracted_at TIMESTAMPTZ, version INT, supersedes TEXT)
  추출된 지식 단위 하나. type: entity/fact/event/decision/action_item/requirement/policy/
  issue/concept. content는 type마다 자유 스키마다 - entity는
  {"name","entity_type","description"}, 나머지는 보통 {"statement": "..."} 형태지만
  추출 결과에 따라 달라질 수 있다. JSONB 필드는 content->>'name' 처럼 접근할 것.
  project_id 컬럼이 이 테이블엔 없다 - sources와 JOIN해서 얻을 것
  (JOIN sources s ON s.source_id = knowledge_objects.source_id).

relations(relation_id TEXT PK, type TEXT, subject_id TEXT REFERENCES knowledge_objects,
          object_id TEXT REFERENCES knowledge_objects, label TEXT, confidence DOUBLE PRECISION,
          created_at TIMESTAMPTZ)
  KnowledgeObject 사이의 typed edge. type: mentions/related_to/caused_by/depends_on/
  references/supersedes/duplicate_of. "특정 사람이 언급된 항목" 같은 질문은 이름이 일치하는
  entity를 먼저 찾고, 그 knowledge_object_id를 relations.object_id로 갖는 행의 subject_id로
  다시 knowledge_objects를 JOIN해서 풀 것.

knowledge_object_embeddings(embedding_id TEXT PK, target_kind TEXT ('knowledge_object'|'chunk'),
                              knowledge_object_id TEXT NULL, chunk_id TEXT NULL, document_id TEXT,
                              embedding_model TEXT, embedding vector(1536), project_id TEXT)
  embedding 컬럼은 절대 SELECT하지 말 것(1536차원 벡터라 응답이 거대해진다) - 이 테이블은
  "이 KO/청크가 임베딩되어 있는지" 존재 여부 확인 용도로만 쓸 것. target_kind에 따라
  knowledge_object_id 또는 chunk_id 중 하나만 채워져 있고 document_id는 항상 있다.

규칙:
- SELECT 또는 WITH ... SELECT만 허용된다 - INSERT/UPDATE/DELETE/DDL은 전부 거부된다.
- 세미콜론으로 이어붙인 여러 문장은 허용되지 않는다 - 한 번에 하나의 조회만.
- 결과는 최대 __MAX_ROWS__행으로 서버에서 강제로 잘린다.
- PostgreSQL 문법(JSONB 연산자 ->/->>, ILIKE, 날짜 산술 등)을 쓸 것.
""".strip()


def describe_schema(max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """`SCHEMA_DESCRIPTION` with the row-limit placeholder filled in - a
    plain `str.replace` rather than `str.format`, since the schema text
    itself is full of literal `{...}` (JSONB shape examples like
    `{"name", ...}`) that `.format()` would otherwise choke on trying to
    interpret as more placeholders."""
    return SCHEMA_DESCRIPTION.replace("__MAX_ROWS__", str(max_rows))


class SqlSearchError(ValueError):
    pass


def _validate_readonly_sql(sql: str) -> str:
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if ";" in stripped:
        raise SqlSearchError("여러 SQL 문은 허용되지 않는다 - SELECT(또는 WITH ... SELECT) 하나만 작성할 것.")
    if not _LEADING_KEYWORD_RE.match(stripped):
        raise SqlSearchError("SELECT 또는 WITH로 시작하는 조회 쿼리만 허용된다.")
    if _FORBIDDEN_KEYWORD_RE.search(stripped):
        raise SqlSearchError("쓰기/DDL 키워드는 허용되지 않는다 - 조회(SELECT)만 가능하다.")
    return stripped


def _json_safe(value: Any) -> Any:
    """Coerces one column value into something `json.dumps` (agent/loop.py's
    `_wrap_untrusted`, which has no `default=`) can actually serialize -
    every other tool here only ever returns plain str/int/float/bool/list/
    dict because its shape is hand-built, but a `sql_search` result column
    can be anything the model's own SELECT projects (a raw `datetime` from
    `extracted_at`, a `Decimal` from an aggregate, a `UUID`, ...)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes, omitted>"
    if isinstance(value, list):
        if len(value) > _EMBEDDING_LIST_THRESHOLD and all(isinstance(v, (int, float)) for v in value):
            return f"<vector, {len(value)} dims, omitted>"
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return str(value)


async def sql_search(sql: str, store: PipelineStore, limit: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """Validates `sql` (raises `SqlSearchError` - a plain `ValueError`
    subclass - if it isn't a single read-only statement), runs it via
    `store.execute_readonly_query()`, and returns
    `{"columns": [...], "rows": [{col: value, ...}, ...], "row_count": N,
    "truncated": bool}`. `truncated` is true when `row_count == limit` - the
    query may have had more matches that got cut off, so the caller should
    narrow it (tighter WHERE, or its own aggregate) rather than assume it
    saw everything."""
    validated = _validate_readonly_sql(sql)
    rows, columns = await store.execute_readonly_query(validated, limit)
    return {
        "columns": columns,
        "rows": [{col: _json_safe(value) for col, value in zip(columns, row, strict=True)} for row in rows],
        "row_count": len(rows),
        "truncated": len(rows) >= limit,
    }
