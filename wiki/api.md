# API 서버 + Job Queue/Worker

`wiki/roadmap.md` 4단계("Ingest와 Process 분리")가 요구하는 "요청을 받는 즉시 응답하고, 무거운
처리는 별도 워커가 넘겨받는" 구조. 이 프로젝트 최초의 HTTP 레이어다 — `extract`/`schedule`은
여전히 순수 CLI 블로킹 실행이고, 이 문서가 다루는 `serve`/`worker`는 별도의 실행 모드다.

```text
클라이언트 → POST /ingest ──► FastAPI(serve) ──► jobs 테이블(Postgres, pending)
                                  │                        │
                                  │ 202 + job_id (즉시)      │ Celery task 전송 (Redis)
                                  ▼                        ▼
                              GET /jobs/{id}          knowledge-forge worker (별도 프로세스, Celery)
                              GET /knowledge-objects        │ run_pipeline(...) 실행
                                  ▲                          ▼
                                  └──────── 결과 저장 (KnowledgeObject/Relation) + jobs.status=succeeded/failed

                              Flower(:5555) ◄── Redis broker 관찰 (task/워커 모니터링 GUI)
```

## 왜 `jobs` 테이블 + Celery/Redis인가

job의 생애주기(상태/결과/에러/타임스탬프, `project_id` 기반 조회)는 여전히 `jobs`
테이블(`storage/schema.sql`)이 진실 소스다 — `POST /ingest`가 이 테이블에 pending row를 쓰고,
`GET /jobs`가 여기서 읽는다. 처음엔 이 테이블 하나와 `SELECT ... FOR UPDATE SKIP LOCKED` 폴링만으로
큐까지 구현했었다(새 인프라를 안 늘리는 게 지금 규모에는 맞다고 판단해서). 이후 운영 중
**GUI 기반 job 모니터링(Flower)**이 필요해지면서, "누가 언제 job을 실행하는가"라는 전송
계층만 Celery+Redis로 옮겼다 — Flower가 들여다볼 브로커/워커가 있어야 하기 때문이다. `jobs`
테이블/`/ingest`/`/jobs` API 계약 자체는 바뀌지 않았다.

## job type: `local_file_ingest` + `{source}_ingest`

`jobs.job_type`이 payload 모양을 구분한다. `POST /ingest`의 `source`(기본값 `"local"`)가
어떤 job_type이 enqueue되는지 결정한다 — `cli.py`의 `extract`/`schedule --source`와 같은
소스 이름을 쓴다:

- `source: "local"`(기본값): 경로 존재를 확인한 뒤 `local_file_ingest`
  (`{"path", "max_tokens", "model", "okf_dir"}`) enqueue. `path`가 필수.
- `source: "slack" | "notion" | "gdrive" | "gmail"`(`collectors.saas.factory.SAAS_COLLECTOR_BUILDERS`의
  키): `"{source}_ingest"`(예: `slack_ingest`, `{"max_tokens", "model", "okf_dir"}` — `path` 없음,
  KF_* env 설정으로 커넥터를 조립하므로) enqueue. 목록에 없는 `source`는 `400`.

`okf_dir`(선택, 기본 `null`)은 `extract`/`schedule`의 `--okf-dir`와 같은 뜻 — 주면
Postgres 저장과 별개로 파일 기반 OKF Knowledge Base도 그 디렉터리(`{project_id}/` 서브폴더)에
기록한다([`okf.md`](okf.md)). job은 1회성 실행이라 `extract`처럼 job이 끝난 뒤 매니페스트/뷰를
한 번만 재생성한다(`schedule`의 매 tick 재생성과는 다름).

`worker.py`의 `_dispatch()`가 `job_type`을 보고 `local_file_ingest`는 `ingest.run_local_ingest()`,
`{source}_ingest`는 `ingest.run_saas_ingest(source, ...)`를 호출한다 — 새 SaaS 커넥터가
`SAAS_COLLECTOR_BUILDERS`에 추가되면 `_dispatch`는 자동으로 그 job_type도 처리한다(하드코딩된
매핑을 늘릴 필요 없음). 큐/워커 인프라 자체(전송 계층, 실패 격리)는 `local_file_ingest` 하나였을
때 그대로다 — 새 job_type 추가가 재설계를 요구하지 않는다는 설계 의도가 그대로 맞아떨어졌다.

## 실행

```bash
# API 서버 - LLM을 호출하지 않으므로 Anthropic 자격증명 불필요
uv run knowledge-forge serve [--host 0.0.0.0] [--port 8000]

# 워커(Celery) - 실제 pipeline을 실행하므로 extract/schedule과 동일하게 Anthropic 자격증명 필요
uv run knowledge-forge worker [--concurrency 4] [--loglevel info]

# 모니터링 GUI (선택) - http://localhost:5555
uv run --extra flower celery -A knowledge_forge.celery_app flower
```

`serve`/`worker` 모두 `KF_DATABASE_URL`이 필수다(`extract`의 `--store`처럼 opt-in이 아님 -
큐/조회가 이 서버의 핵심 기능이라 DB 없이는 의미가 없다). `serve`는 `KF_DATABASE_URL`이 없어도
뜨지만(`GET /health`만 정상), `/ingest`/`/jobs`/`/knowledge-objects`/`/relations`는 503을
반환한다. `worker`는 `KF_DATABASE_URL`이 없으면 즉시 에러 종료한다. 둘 다 `KF_REDIS_URL`(기본
`redis://localhost:6379/0`)로 Celery 브로커/결과 백엔드를 가리킨다 - `POST /ingest`가 job을
실제로 워커에 전달하려면 Redis가 떠 있어야 한다.

docker-compose에는 `redis`/`api`/`worker`/`flower` 서비스가 있다(`app`처럼 `build: .`/`depends_on:
postgres(healthy)`, `api`/`worker`는 `redis(healthy)`도 추가):

```bash
docker compose up -d postgres redis api worker flower
curl -X POST localhost:8000/ingest -H 'Content-Type: application/json' -d '{"path": "/app/input/sample.md"}'
curl localhost:8000/jobs/<job_id>
curl localhost:8000/knowledge-objects
open http://localhost:5555  # Flower - task/워커 상태 GUI
```

## 자동 스케줄링 (`KF_SCHEDULED_SOURCES`)

`serve` 프로세스 자체가 (`api/app.py`의 lifespan에서) 소스별 반복 job을 이 `jobs` 큐에
enqueue할 수도 있다 — `knowledge-forge schedule`처럼 별도 프로세스를 띄우지 않고도
`serve`를 시작하는 것만으로 주기적 수집이 시작된다. 자세한 설명은
[`scheduling.md`](scheduling.md)의 "`serve` 자동 스케줄링" 절 참고. 기본값은 비활성화
(`KF_SCHEDULED_SOURCES` 미설정) — 명시적으로 설정하지 않는 한 이 절 이전까지의 동작(요청이
와야 enqueue)과 완전히 동일하다.

## 엔드포인트

FastAPI가 자동 생성하는 대화형 문서도 있다: `GET /docs`(Swagger UI), `GET /redoc`, `GET
/openapi.json`. 라우터별로 태그(Health/Ingest/Jobs/Sources/Documents/Knowledge
Objects/Relations)가 붙어 있어 Swagger UI에서 리소스별로 묶여 보인다.

| 메서드/경로 | 설명 |
|---|---|
| `GET /health` | liveness, DB 의존 없음(컨테이너 healthcheck용) |
| `POST /ingest` | `{"source": "<local(기본)\|slack\|notion\|gdrive\|gmail>", "path": "<local일 때만 필수>", "max_tokens": <선택>, "model": "<선택>", "okf_dir": "<선택>", "project_id": "<선택, 기본 "default">"}` → `local_file_ingest` 또는 `{source}_ingest` job enqueue, `202` + `{job_id, status}` |
| `GET /jobs/{job_id}` | job 상태/결과 조회, 없으면 `404` |
| `GET /jobs?status=&project_id=` | job 목록(페이지네이션 `limit`/`offset`) |
| `GET /sources?project_id=&limit=&offset=` | 저장된 Source 조회 |
| `GET /sources/{source_id}` | 단건 조회, 없으면 `404` |
| `GET /documents?project_id=&source_id=&limit=&offset=` | 저장된 Document 조회 |
| `GET /documents/{document_id}` | 단건 조회(전체 `elements` 포함), 없으면 `404` |
| `GET /knowledge-objects?source_id=&type=&limit=&offset=` | 저장된 KnowledgeObject 조회 |
| `GET /knowledge-objects/{id}` | 단건 조회, 없으면 `404` |
| `GET /relations?subject_id=&object_id=&type=` | Relation 조회 |

## 멀티테넌시(`project_id`)

`sources`/`documents`/`jobs` 세 테이블 모두 `project_id TEXT NOT NULL DEFAULT 'default'` 컬럼을
가진다(`storage/schema.sql`). 한 프로젝트가 수집한 데이터가 다른 프로젝트에 섞이지 않도록 하는
테넌시 키로, `Source`가 만들어지는 시점(`pipeline._make_source`)에 한 번 태그되면
`SourceRef.project_id`를 통해 `Document`/`KnowledgeObject`까지 그대로 전파된다(`documents.project_id`는
연결된 `sources.project_id`에서 파생되어 저장된다 - `Document` 모델 자체에는 별도 필드가 없다).

**진입점별로 `project_id`를 지정하는 방법**:

| 진입점 | 지정 방법 |
|---|---|
| `POST /ingest` | body에 `"project_id": "..."` (생략 시 `"default"`) |
| `knowledge-forge extract` | `--project-id` |
| `knowledge-forge schedule` | `--project-id` |
| `GET /sources`, `/documents`, `/jobs` | 쿼리 파라미터 `?project_id=...`로 해당 프로젝트만 필터링 (생략 시 전체) |

**알려진 한계 - `project_id`는 필터이지, 접근 제어가 아니다**: 이 API에는 인증/인가 계층이
전혀 없다(`api/deps.py`는 DB 연결 여부만 확인한다). 즉 어떤 클라이언트든 임의의 `project_id`로
`/ingest`를 호출하거나 다른 프로젝트의 `project_id`로 `/sources`·`/documents`·`/jobs`를 조회할
수 있다 - 지금 단계에서는 "UI가 프로젝트별로 나눠서 보여줄 수 있는 키"이지 "다른 프로젝트가
서로의 데이터를 못 보게 막는 보안 경계"가 아니다. 실제 멀티테넌트 격리(요청자를 프로젝트에
매핑하는 API 키/JWT 인증, 각 요청을 그 프로젝트로 강제 스코핑)는 아직 구현되어 있지 않은
후속 과제다.

## Job 상태 전이

```text
pending --(Celery worker가 _execute_job에서 mark_running)--> running --(성공)--> succeeded
                                         │
                                         └──(예외)──> failed (jobs.error에 메시지 기록, 예외 재raise로 Celery/Flower에도 실패로 표시)
```

`worker.py`의 실패 격리는 `scheduling/scheduler.py`의 tick과 같은 철학이다: 한 job의 예외는 그
job을 `failed`로 표시할 뿐 워커 프로세스 자체를 죽이지 않는다 - 다음 job이 정상적으로 처리된다.
재시도(retry)는 아직 없다 - 처리 도중 워커가 죽으면 job은 `running`에 멈춘 채 남는다(이전
poll 기반 구현과 동일한 한계).

## 알려진 한계 (다음 후속 과제)

- **요청당 새 연결, 커넥션 풀 없음** (`api/deps.py`) — `scheduling/scheduler.py`의 "tick마다 새
  연결" 원칙을 그대로 따른 선택이라 단순하고 연결 끊김에 자동 복구되지만, 동시 요청이 많아지면
  처리량이 문제될 수 있다. `psycopg_pool`로 옮기는 건 실제 병목이 확인된 뒤의 후속 과제.
- **재시도 없음** — 실패한 job은 `failed`로 끝나고 자동 재큐잉되지 않는다(Celery 자체는 재시도
  기능이 있지만 아직 켜지 않았다). 워커가 처리 도중 죽는 경우도 마찬가지로 job이 `running`에
  멈춘 채 남는다.
- **`/ingest`의 경로 존재 검증은 enqueue 시점 스냅샷** — 워커가 실제로 job을 집어들 때 파일이
  사라졌으면(레이스) job은 `failed`로 끝난다(정상적인 실패 격리 경로, 별도 처리 불필요).
- **SaaS ingest job은 실제 계정으로 라이브 검증되지 않았다** — CLI `--source`와 마찬가지로
  모킹 테스트만 거쳤다(`tests/test_worker_dispatch_routing.py`, `tests/test_ingest.py`).
