# API 서버 + Job Queue/Worker

`wiki/roadmap.md` 4단계("Ingest와 Process 분리")가 요구하는 "요청을 받는 즉시 응답하고, 무거운
처리는 별도 워커가 넘겨받는" 구조. 이 프로젝트 최초의 HTTP 레이어다 — `extract`/`schedule`은
여전히 순수 CLI 블로킹 실행이고, 이 문서가 다루는 `serve`/`worker`는 별도의 실행 모드다.

```text
클라이언트 → POST /ingest ──► FastAPI(serve) ──► jobs 테이블(Postgres, pending)
                                  │                        │
                                  │ 202 + job_id (즉시)      │ SELECT ... FOR UPDATE SKIP LOCKED
                                  ▼                        ▼
                              GET /jobs/{id}          knowledge-forge worker (별도 프로세스)
                              GET /knowledge-objects        │ run_pipeline(...) 실행
                                  ▲                          ▼
                                  └──────── 결과 저장 (KnowledgeObject/Relation) + jobs.status=succeeded/failed
```

## 왜 Postgres job table인가

이미 docker-compose에 Postgres가 떠 있다(`storage.md`). Redis/Celery 같은 새 인프라를 추가하지
않고 `jobs` 테이블 하나(`storage/schema.sql`)와 `SELECT ... FOR UPDATE SKIP LOCKED` 폴링으로
큐를 구현한다 — 여러 워커가 동시에 `claim_next()`를 호출해도 서로 다른 job을 가져가는 것을
`tests/storage/test_job_store.py::test_claim_next_is_concurrency_safe_across_connections`가
검증한다. 처리량이 실제로 문제가 되면 그때 Redis/전용 큐로 옮기면 된다 — 지금 규모에서는
과설계다.

## v1 범위: `local_file_ingest` job type 하나만

`jobs.job_type`이 payload 모양을 구분한다. 지금은 이미 동작하는 `LocalFileCollector` 경로를
검증하는 `local_file_ingest`(`{"path", "max_tokens", "model"}`) 하나만 있다. **SaaS 소스는
아직 여기 연결되어 있지 않다** — 로드맵 1단계(SaaS CLI/OAuth 연동)가 끝나야 `slack_ingest` 같은
새 job_type을 추가할 수 있다. 큐/워커 자체는 그때 다시 설계할 필요 없이 `worker.py`의
`_dispatch`에 분기 하나만 추가하면 된다.

## 실행

```bash
# API 서버 - LLM을 호출하지 않으므로 Anthropic 자격증명 불필요
uv run knowledge-forge serve [--host 0.0.0.0] [--port 8000]

# 워커 - 실제 pipeline을 실행하므로 extract/schedule과 동일하게 Anthropic 자격증명 필요
uv run knowledge-forge worker [--poll-interval-seconds 2.0]
```

둘 다 `KF_DATABASE_URL`이 필수다(`extract`의 `--store`처럼 opt-in이 아님 - 큐/조회가 이 서버의
핵심 기능이라 DB 없이는 의미가 없다). `serve`는 `KF_DATABASE_URL`이 없어도 뜨지만(`GET /health`만
정상), `/ingest`/`/jobs`/`/knowledge-objects`/`/relations`는 503을 반환한다. `worker`는
`KF_DATABASE_URL`이 없으면 즉시 에러 종료한다.

docker-compose에는 `api`/`worker` 서비스가 각각 있다(`app`처럼 `build: .`/`depends_on:
postgres(healthy)`):

```bash
docker compose up -d postgres api worker
curl -X POST localhost:8000/ingest -H 'Content-Type: application/json' -d '{"path": "/app/input/sample.md"}'
curl localhost:8000/jobs/<job_id>
curl localhost:8000/knowledge-objects
```

## 엔드포인트

FastAPI가 자동 생성하는 대화형 문서도 있다: `GET /docs`(Swagger UI), `GET /redoc`, `GET
/openapi.json`. 라우터별로 태그(Health/Ingest/Jobs/Knowledge Objects/Relations)가 붙어 있어
Swagger UI에서 리소스별로 묶여 보인다.

| 메서드/경로 | 설명 |
|---|---|
| `GET /health` | liveness, DB 의존 없음(컨테이너 healthcheck용) |
| `POST /ingest` | `{"path": "<로컬 경로>"}` → 경로 존재 확인 후 `local_file_ingest` job enqueue, `202` + `{job_id, status}` |
| `GET /jobs/{job_id}` | job 상태/결과 조회, 없으면 `404` |
| `GET /jobs?status=` | job 목록(페이지네이션 `limit`/`offset`) |
| `GET /knowledge-objects?source_id=&type=&limit=&offset=` | 저장된 KnowledgeObject 조회 |
| `GET /knowledge-objects/{id}` | 단건 조회, 없으면 `404` |
| `GET /relations?subject_id=&object_id=&type=` | Relation 조회 |

## Job 상태 전이

```text
pending --(worker가 claim_next())--> running --(성공)--> succeeded
                                         │
                                         └──(예외)──> failed (jobs.error에 메시지 기록)
```

`worker.py`의 실패 격리는 `scheduling/scheduler.py`의 tick과 같은 철학이다: 한 job의 예외는 그
job을 `failed`로 표시할 뿐 워커 프로세스 자체를 죽이지 않는다 - 다음 job이 정상적으로 폴링된다.

## 알려진 한계 (다음 후속 과제)

- **요청당 새 연결, 커넥션 풀 없음** (`api/deps.py`) — `scheduling/scheduler.py`의 "tick마다 새
  연결" 원칙을 그대로 따른 선택이라 단순하고 연결 끊김에 자동 복구되지만, 동시 요청이 많아지면
  처리량이 문제될 수 있다. `psycopg_pool`로 옮기는 건 실제 병목이 확인된 뒤의 후속 과제.
- **워커는 poll 기반**(`--poll-interval-seconds`, 기본 2초) — job이 없을 때 최대 poll 간격만큼
  지연될 수 있다. `LISTEN`/`NOTIFY`로 즉시 깨우는 건 지연이 실제 문제가 될 때의 후속 과제.
- **`local_file_ingest` job type 하나뿐** — 위 "v1 범위" 참고.
- **`/ingest`의 경로 존재 검증은 enqueue 시점 스냅샷** — 워커가 실제로 job을 집어들 때 파일이
  사라졌으면(레이스) job은 `failed`로 끝난다(정상적인 실패 격리 경로, 별도 처리 불필요).
