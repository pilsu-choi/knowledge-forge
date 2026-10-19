---
type: Design Note
title: 읽기 전용 MCP 서버와 그래프 읽기
description: 두 번째 소비 Agent용 MCP 서버(lookup_entity/get_object/neighbors/search)와 그 아래의 관계 탐색(O3·O6).
tags: [ontology, mcp, graph, project_id]
status: stable
generated:
  by: claude-opus-5.5
  at: 2026-10-19
sources:
  - resource: improvement-plan-2026-09-15.md
    title: 개선 필요 사항 (O3, O6)
---

# 읽기 전용 MCP 서버와 그래프 읽기 (2026-10-19)

[`improvement-plan-2026-09-15.md`](improvement-plan-2026-09-15.md)의 O3(relations 읽기)와 O6(두 번째
소비자 인터페이스)을 구현했다. 엔티티·mention 테이블(O1)은 [`knowledge-object-schema.md`](knowledge-object-schema.md),
[`storage.md`](storage.md) 참고.

## 결론

- 그래프 읽기는 `search/graph.py` 한 곳이다. 에이전트 도구, MCP 서버, HTTP 라우트가 모두 그 위의 얇은 어댑터다.
- MCP 서버는 **기동할 때 프로젝트 하나에 고정**된다. 어떤 도구도 `project_id` 인자를 받지 않는다. 저장소도
  같은 범위를 한 번 더 강제한다(다른 프로젝트의 객체는 "없음"으로 읽힌다).
- 관계 타입 어휘는 좁히지 않았다. 추출이 실제로 내는 타입은 `mentions`/`related_to`/`caused_by` 셋이고,
  끝점 타입은 `extraction/outcome.py`가 구조적으로 보장한다(`mentions`의 object는 항상 entity). 술어는
  `label`에 남아 탐색 결과에 그대로 나온다. 엔드포인트 제약을 저장 시 검증하는 건 타입을 늘릴 때 같이 한다.
- 현재 개발 DB는 청크만 적재해 KO가 없다. 이 도구들은 KO 추출 이후에 결과가 나온다.

## 그래프 읽기 (`PostgresStore`)

| 메서드 | 동작 |
|---|---|
| `neighbors(object_id, project_id, depth, relation_types, limit)` | relations를 양방향 BFS. 홉당 쿼리 1개. 각 객체는 처음 도달한 간선(최단 경로)과 함께 한 번만. 홉 안에서는 confidence 높은 간선 우선 |
| `list_entity_objects(entity_id, roles, limit)` | 엔티티 → KO 펼치기(`entity_mentions` PK), `(role, KO)` 최신순 |
| `get_knowledge_object(id, project_id=)` / `list_knowledge_objects(project_id=)` | O6에서 지적된 `project_id` 필터 추가 |

`neighbors`가 호출 인자와 무관하게 지키는 것:

- **프로젝트 경계**: 출발 객체의 프로젝트를 절대 벗어나지 않는다. `project_id`를 주면 출발 객체도 그 안에 있어야 한다.
- **대체된 객체 제외**: 어떤 객체가 `supersedes`하는 옛 버전은 돌려주지 않는다(`idx_ko_supersedes`).
- **기본 관계 타입**: `TRAVERSAL_RELATION_TYPES`(mentions/related_to/caused_by/depends_on/references).
  `supersedes`·`duplicate_of`는 같은 주장의 버전·사본 사이 기록이라 따라가도 같은 문장만 다시 나온다.
- **상한**: depth 3, 객체 200(`search/graph.py`가 clamp). 모르는 관계 타입은 `ValueError`.

결과 형식(`search/graph.py::neighbors`): `{"object": 출발, "neighbors": [{depth, direction, from, relation:{type,label,confidence}, object}]}`.
`direction`은 `out`이면 `from --type--> object`, `in`이면 `object --type--> from`이다.

## 노출

| 소비자 | 표면 | 범위 |
|---|---|---|
| 내장 에이전트 | `lookup_entity`, `neighbors` 도구(DB가 있으면 minimal/full 둘 다) | 세션 `project_id`(없으면 전체, 단 탐색은 출발 객체 프로젝트 안) |
| HTTP | `GET /entities?name=`, `GET /relations/expand?object_id=&depth=&relation_types=` | 쿼리 `project_id` - 필터일 뿐 인증 없음([`api.md`](api.md)) |
| MCP | 아래 | 기동 시 고정 |

## MCP 서버 (`mcp_server.py`)

공식 `mcp` 파이썬 SDK 2.x(`MCPServer`, 옛 FastMCP)로 만들었고 stdio 전송이다.

```bash
KF_DATABASE_URL=postgresql://... knowledge-forge mcp --project-id default   # 또는 KF_MCP_PROJECT_ID
```

Claude Desktop/Claude Code 같은 클라이언트 설정 예:

```json
{"mcpServers": {"knowledge-forge": {"command": "knowledge-forge", "args": ["mcp", "--project-id", "default"],
  "env": {"KF_DATABASE_URL": "postgresql://kf:kf@localhost:15432/knowledge_forge"}}}}
```

| 도구 | 인자 | 결과 |
|---|---|---|
| `lookup_entity` | `name`, `entity_type?`, `limit=5` | 엔티티 목록, 각자 언급한 KO와 role |
| `get_object` | `object_id` | KO 하나(타입별 content, confidence, chunk_ids, 출처). 없거나 다른 프로젝트면 도구 오류 |
| `neighbors` | `object_id`, `depth=1`, `relation_types?`, `limit=20` | 위 결과 형식 |
| `search` | `query`, `limit=10` | 에이전트 `search` 도구와 같은 결과(`search/retrieve.py`) |

- 도구 로직은 에이전트 도구의 핸들러(`agent/tools.py::read_only_tools`)를 그대로 쓴다. limit clamp와 결과
  다듬기가 한 곳에만 있다. 이 모듈은 SDK가 스키마를 만들 타입 시그니처와 오류 변환만 맡는다.
- 모든 도구에 `readOnlyHint` 주석을 단다. 쓰기 도구는 없다.
- `search`는 규칙만 쓴다(`provider=None`). 요청마다 LLM을 부르면 모든 소비자가 이 서버의 모델 비용을 내게 된다.
  벡터 경로의 질의 임베딩은 `KF_EMBEDDING_*` 설정대로 호출된다.
- 프로젝트 여러 개가 필요하면 프로세스를 여러 개 띄운다. 한 프로세스가 여러 프로젝트를 고르게 하면 그 선택이
  다시 요청 인자가 된다.

## 남은 문제

- HTTP API의 `project_id`는 여전히 필터일 뿐이다. `GET /relations`는 프로젝트 필터도 없다.
- 원격(streamable-http) 전송과 인증은 없다. stdio는 클라이언트가 서버 프로세스를 띄우는 로컬 신뢰 모델이다.
- `sql_search`의 스키마 설명(`search/sql.py`)에는 `entities`/`entity_mentions`가 아직 없다.
