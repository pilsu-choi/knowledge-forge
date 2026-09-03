# Agent 검색 (설계 문서)

`Agents.md`가 요구하는 "고민 사항을 wiki에 기록"에 따라, 지금까지 쌓아온 Knowledge Base
(PostgreSQL의 `knowledge_objects`/`relations`/`knowledge_object_embeddings`, 그리고 OKF 파일
트리)를 **Claude Code 같은 에이전트가 검색 도구로 사용할 수 있게 만드는** 남은 작업을 정리해
둔다. `wiki/roadmap.md`의 제외 범위 표가 "Vector DB/Search-RAG를 지금 만들지 않는 이유는
스코프 관리이지 설계가 막아서가 아니다"라고 적어 둔 것의 구체적인 후속 설계가 이 문서다.

**상태 요약**:

| 구성 요소 | 상태 |
|---|---|
| pgvector 스키마(`knowledge_object_embeddings`) | **구현됨** — [`embeddings.md`](embeddings.md) |
| 임베딩 생성 파이프라인(`--embed`, `knowledge-forge embed`) | **구현됨** — [`embeddings.md`](embeddings.md) |
| `POST /search/vector` (vector_search) | 설계만, 미구현 |
| `GET /relations/expand` (graph_search) | 설계만, 미구현 |
| `GET /okf/search` (file_search, OKF 트리 기반) | 설계만, 미구현 |
| `knowledge-forge mcp-server` | 설계만, 미구현 |
| Claude Code용 검색 skill | 설계만, 미구현 |

이 문서의 나머지 절 중 "구현됨"이라고 표시되지 않은 것은 전부 설계 스케치이며, 실제로 동작하는
코드가 없다. 코드로 이미 존재하는 부분(스키마/파이프라인)은 이 문서가 아니라
[`embeddings.md`](embeddings.md)가 최신 상태의 기준이다 — 여기 남은 설명은 그 설계가 실제로
왜 이렇게 구현됐는지를 잇는 배경 정도로만 참고할 것.

## pgvector 스키마 설계 — **구현됨**, `embeddings.md` 참고

`KnowledgeObject`를 벡터로 저장하는 스키마(`knowledge_object_embeddings` 테이블, HNSW/cosine
인덱스)는 구현이 끝났다. 설계 근거(왜 별도 테이블인지, 왜 HNSW인지)와 정확한 DDL은
[`embeddings.md`](embeddings.md)의 "스키마" 절에 있다 — 여기서 중복 서술하지 않는다.

## 임베딩 생성 파이프라인 설계 — **구현됨**, `embeddings.md` 참고

`EmbeddingProvider` 추상화, `--embed` 플래그, `knowledge-forge embed` 백필 명령, staleness
재임베딩을 지금 자동화하지 않은 이유까지 전부 [`embeddings.md`](embeddings.md)에 있다.

## 검색 API 엔드포인트 설계 (미구현)

에이전트가 Knowledge Base를 검색할 진입점은 세 가지 축으로 나눠 설계한다 — 벡터(의미)
검색, 그래프(관계) 탐색, 파일(OKF 트리) 검색. 셋을 하나의 만능 엔드포인트로 합치지 않는
이유는 [`api.md`](api.md)가 이미 세운 패턴을 따른 것이다: `GET /knowledge-objects` 같은
Postgres 기반 라우터와 `GET /okf/*` 같은 파일시스템 기반 라우터가 서로 다른 의존성
(`get_read_store` vs `get_okf_root`)을 쓰는 것처럼, 검색도 자신이 실제로 무엇을 인덱스로
쓰는지에 따라 엔드포인트를 나누는 편이 각 경로의 "무엇이 있어야 동작하는가"(Postgres+pgvector
vs OKF 디렉터리)를 흐리지 않는다.

### `POST /search/vector` (vector_search)

`knowledge_object_embeddings`의 HNSW 인덱스를 실제로 읽는 첫 번째 소비자. 설계 스케치:

```
POST /search/vector
{
  "query": "이번 분기 배포 일정 관련 결정",
  "project_id": "default",       // 선택, 필터
  "type": ["Decision", "Issue"], // 선택, KnowledgeObject.type 필터
  "top_k": 10,
  "embedding_model": "text-embedding-3-small"  // 생략 시 KF_EMBEDDING_MODEL
}
```

- 쿼리 텍스트를 요청 시점에 `get_embedding_provider().embed([query])`로 임베딩한 뒤,
  `ORDER BY embedding <=> :query_vector LIMIT :top_k`로 조회 — `project_id`/`type` 필터는
  `knowledge_object_embeddings.project_id`(역정규화된 컬럼, join 없이 필터 가능)와
  `knowledge_objects.type`를 WHERE 절에 추가.
- 응답은 `GET /knowledge-objects`와 같은 `KnowledgeObjectResponse` 모양에 `score`(cosine
  거리 또는 1-거리로 정규화한 유사도)만 얹는다 — 검색 결과도 결국 KnowledgeObject이므로
  별도 응답 스키마를 새로 만들 이유가 없다는 판단.
- `embedding_model`을 요청에 받는 이유: `knowledge_object_embeddings`의 PK가
  `(knowledge_object_id, embedding_model)`이라 여러 모델의 벡터가 동시에 존재할 수 있다
  ([`embeddings.md`](embeddings.md) "왜 별도 테이블인가" 참고) — 검색도 어느 모델의
  벡터 공간에서 비교할지 명시해야 한다. 생략 시 `KF_EMBEDDING_MODEL`(현재 활성 모델)로
  기본값을 잡는다.
- **아직 결정 안 된 것**: 임베딩이 없는 KnowledgeObject(백필 전, 또는 `--no-embed`로 저장된
  것)를 검색 결과에서 어떻게 다룰지 — 그냥 안 나오는 것으로 둘지, 경고를 응답에 실을지는
  실제 사용 패턴을 보고 정할 문제라 아직 정하지 않았다.

### `GET /relations/expand` (graph_search)

벡터 검색으로 KnowledgeObject 하나를 찾은 다음, 그 주변 그래프(무엇과 연결되어 있는지)를
따라가는 용도 — `relations` 테이블이 이미 `subject_id`/`object_id`로 typed edge를 표현하고
있으므로([`storage.md`](storage.md) 스키마 표), 새 저장 구조 없이 조회 쿼리만 추가하면 된다.

```
GET /relations/expand?knowledge_object_id=ko_123&depth=1&type=SUPERSEDES,DUPLICATE_OF
```

- `depth=1`(기본)은 해당 KO를 `subject_id` 또는 `object_id`로 갖는 `relations` 행만 반환 —
  `GET /relations?subject_id=` 조회를 양방향으로 합친 것과 동일. `depth>1`은 인접 KO로부터
  다시 확장하는 재귀 탐색이라, N+1 쿼리 대신 Postgres의 `WITH RECURSIVE`로 구현하는 것을
  1차안으로 잡아 두었다(아직 검증 안 함) — 순환(cycle) 방지를 위해 방문한 `knowledge_object_id`
  집합을 추적해야 한다는 점이 SQL 재귀 CTE에서 특히 신경 쓸 부분.
- 응답은 `GET /okf/projects/{project_id}/graph`가 이미 만들고 있는 "documents/objects/edges,
  Cytoscape 노드/엣지" 모양([`api.md`](api.md) 엔드포인트 표)을 그대로 재사용하는 안을
  1순위로 본다 — `/ui`의 그래프 탭이 이미 그 모양을 소비하고 있어, 프런트엔드가 두 가지
  그래프 응답 모양을 다룰 필요가 없어진다.

### `GET /okf/search` (file_search, OKF 트리 기반)

Postgres 없이 OKF 파일 트리만으로 동작하는 검색 — `roadmap.md`의 "DB 의존성 없이 OKF 파일
트리만으로 에이전트 컨텍스트를 구성하는 실험"([`okf.md`](okf.md))이 이미 "grep + frontmatter
스캔 + index.md/log.md가 곧 검색 표면"이라고 정의해 둔 것을, HTTP 엔드포인트 하나로 감싸는
정도의 설계다.

```
GET /okf/projects/{project_id}/search?q=배포+일정&type=Decision
```

- `okf/reader.py`가 이미 갖고 있는 `list_projects`/`read_manifest`/`list_objects_by_type`
  위에, 각 오브젝트 파일의 frontmatter(summary/topics/status)와 본문을 대상으로 한 단순
  부분 문자열 매칭을 얹는 것을 1차안으로 본다 — pgvector처럼 의미 검색이 아니라, `grep -r`을
  구조화된 결과로 감싸는 수준. Postgres가 없는 순수 파일 기반 배포(OKF만 쓰는 사용자)에서도
  검색이 가능하게 하는 것이 목적이라, `/search/vector`와 품질을 경쟁시킬 필요는 없다.
- **미해결**: 오브젝트 수가 많아지면(매 요청마다 트리 전체를 스캔하는) `manifest.yaml` 기반
  선형 스캔이 느려질 수 있다는 점 — `okf/reader.py`가 지금 `list_objects_by_type`에서 이미
  쓰는 "트리 스캔 후 재생성" 패턴([`okf.md`](okf.md))을 그대로 검색에도 쓸지, 아니면 별도
  인덱스 파일을 캐시할지는 실제 트리 크기를 보고 결정할 문제.

## `knowledge-forge mcp-server` 설계 (미구현)

Claude Code 같은 MCP 클라이언트가 붙을 수 있도록, 위 세 검색 엔드포인트를 MCP tool로
감싸는 새 CLI 명령. `serve`(FastAPI, HTTP)와는 별도 진입점으로 설계한다 — MCP는 stdio
기반 프로토콜이라 웹 서버 프로세스와 생애주기가 다르고(에이전트 클라이언트가 서브프로세스로
직접 구동), `cli.py`의 기존 명령들(`worker`, `serve`)이 이미 "같은 코드베이스, 다른 실행
모드"를 여러 개 두고 있는 패턴을 그대로 잇는다.

```bash
uv run knowledge-forge mcp-server --project-id default
```

- 노출할 tool 후보(모두 위 세 엔드포인트 로직을 그대로 호출하는 얇은 wrapper로 설계):
  `search_knowledge_objects`(vector_search), `expand_relations`(graph_search),
  `search_okf_files`(file_search, `--okf-dir`가 설정된 경우만).
- HTTP 라우터(`api/routes/`)와 MCP tool 핸들러가 같은 하부 함수를 호출하도록 로직을
  `api/routes/*.py`가 아니라 그 아래의 서비스 함수로 한 번 더 분리해야 한다는 게 현재
  판단이다(`api/routes/knowledge.py`가 지금 라우터 함수 안에 직접 쿼리를 넣고 있는 것과
  다른 구조) — 지금은 이 리팩터를 하지 않은 채로 남겨 두었다.
- 인증/자격 증명: MCP 서버 자체는 LLM을 호출하지 않으므로(검색만 수행) Anthropic/OpenAI
  키가 필요 없다 — `KF_DATABASE_URL`(벡터/그래프 검색용)과 `KF_OKF_DIR`(파일 검색용) 중
  최소 하나만 있으면 된다는 게 `serve`의 "Postgres 없어도 OKF만으로는 뜬다" 원칙
  ([`api.md`](api.md))과 일치한다.

## Claude Code용 검색 skill 설계 (미구현)

MCP 서버가 서고 나면, 그 tool들을 Claude Code 세션이 자연스럽게 쓰도록 안내하는 skill
문서(`.claude/skills/` 아래) — "언제 이 프로젝트의 Knowledge Base를 검색해야 하는지"(예:
과거 결정/이슈를 참조해야 하는 질문)와 "어떤 tool을 어떤 순서로 쓰는지"(벡터 검색으로 후보를
좁히고, 필요하면 관계 확장으로 근거를 넓히는 흐름)를 짧게 서술하는 정도로 구상 중이다. MCP
서버 자체가 없는 지금은 skill을 먼저 만들 이유가 없어 뒤로 미룬다.

## 왜 지금 여기서 멈추는가

이번 pgvector/임베딩 구현이 끝난 시점에서 판단한 우선순위: 검색이 실제로 값어치가 있으려면
"쌓인 벡터를 읽는 최소 한 경로"(`/search/vector`)가 가장 먼저 필요하고, 그래프 확장·OKF
검색·MCP·skill은 그 위에 얹는 순서가 자연스럽다. 이번 작업 범위를 "임베딩이 쌓이는 것"까지로
좁힌 이유는 [`roadmap.md`](roadmap.md)가 이미 세운 스코프 관리 원칙과 같다 — 검색 엔드포인트를
서두르는 대신, 검색이 읽을 데이터(임베딩)가 먼저 정확하게 쌓이는지부터 확인하고 싶었다.
