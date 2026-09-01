# 스케줄링

`knowledge-forge extract`는 1회성 실행이다. 주기적으로 같은 경로를 계속 증분 동기화하고
싶다면 `knowledge-forge schedule`을 쓴다 — 별도 OS cron/systemd timer 없이, 하나의
장기 실행 프로세스 안에서 `APScheduler`(`AsyncIOScheduler`)가 asyncio 이벤트 루프 위의
백그라운드 job으로 반복한다.

```bash
uv run knowledge-forge schedule <path> \
  [--interval-seconds 600] [--max-tokens 4000] [--model claude-opus-5] [--store/--no-store]
```

포그라운드 프로세스로 실행되며(내부적으로 `asyncio.run(run_scheduled_sync(...))`), Ctrl-C로
정상 종료한다. `--interval-seconds`의 기본값은 600초(10분)다
(`scheduling/scheduler.py`의 `DEFAULT_INTERVAL_SECONDS`).

## 동작 방식 (`src/knowledge_forge/scheduling/scheduler.py`)

`run_scheduled_sync(root, interval_seconds=DEFAULT_INTERVAL_SECONDS, max_tokens=4000, model=None,
store_factory=None, state_path=None)`(async 함수)가:

1. `LocalFileCollector`/`ParserRegistry`/`AdaptiveChunker`/`get_extractor()`(활성
   `KF_LLM_PROVIDER`에 맞는 Extractor, [`llm-providers.md`](llm-providers.md) 참고)를
   **한 번만** 생성해 재사용한다(매 tick마다 다시 만들지 않음).
2. `build_tick(...)`으로 매 tick마다 실행할 **async** 함수를 만든다 — 한 tick 안에서 한 문서의
   여러 청크/표/이미지에 대한 LLM 호출은 `run_pipeline`이 동시 실행한다
   ([`architecture.md`](architecture.md)의 Knowledge Extraction 절 참고).
3. `AsyncIOScheduler.add_job(tick, "interval", seconds=interval_seconds,
   next_run_time=datetime.now(), id=SYNC_JOB_ID)`로 등록 — `next_run_time=datetime.now()`가
   시작 즉시 1회 실행 후 이후 `interval_seconds`마다 반복되게 한다. 고정된 `id=SYNC_JOB_ID`
   ("sync_tick")로 등록해두는 이유는 아래 수동 트리거가 `scheduler.modify_job(SYNC_JOB_ID, ...)`로
   이 job을 다시 찾을 수 있게 하기 위함이다. `scheduler.start()`는 즉시 반환하고,
   `run_scheduled_sync`는 `await asyncio.Event().wait()`로 이벤트 루프를 계속 돌려 tick들이
   백그라운드에서 실행되게 한다(Ctrl-C가 `KeyboardInterrupt`로 이 대기를 깨운다).

## 수동 트리거 — OS 시그널 또는 HTTP

인터벌을 기다리지 않고 지금 바로 한 번 더 동기화하고 싶을 때 쓰는 방법이 두 가지 있다. 둘 다
결국 같은 함수(`trigger_now(scheduler)`)를 호출하며, 이 함수는 tick 함수를 직접 부르지 않고
`scheduler.modify_job(SYNC_JOB_ID, next_run_time=datetime.now())`로 **기존 job을 재스케줄**한다
— 이미 tick이 실행 중이면 APScheduler의 executor가 그대로 직렬화해주므로, 수동 트리거 때문에
두 tick이 동시에 도는 일이 없다. job의 이후 실행 시각은 interval trigger의 원래 시작 시각
기준으로 계속 계산되므로, 수동 트리거가 앞으로의 주기 자체를 밀지는 않는다 — 그 순간 한 번 더
실행될 뿐이다.

### 1. `kill -USR1 <pid>`

`run_scheduled_sync`는 시작 시 (SIGUSR1을 지원하는 POSIX 플랫폼에서)
`loop.add_signal_handler(signal.SIGUSR1, ...)`로 `trigger_now(scheduler)`를 등록하고, 프로세스
PID를 로그로 남긴다. 다른 터미널에서 `kill -USR1 <pid>`를 보내면 즉시 한 번 더 동기화한다.
Windows처럼 `SIGUSR1`/`loop.add_signal_handler`를 지원하지 않는 플랫폼에서는 핸들러 등록을
건너뛰고 인터벌 기반 동작만 한다(이 경로가 없어도 스케줄러 자체와 아래 HTTP 트리거는 정상
동작한다).

### 2. `POST /trigger` (HTTP)

시그널을 보낼 수 없는 환경(원격 오케스트레이션, 컨테이너 사이드카 등)을 위해
`build_trigger_app(scheduler)`가 최소한의 FastAPI 앱을 만든다 — `POST /trigger`가
`trigger_now(scheduler)`를 호출하고 `{"status": "triggered", "next_run_time": ...}`을 돌려주며,
`GET /health`는 다음 실행 예정 시각만 조회한다. `knowledge_forge.api.app`(`serve`/`worker`가 쓰는
job queue 기반 API, [`api.md`](api.md))와는 **별개의 앱**이다 — `schedule`은 그 프로세스와
아무 관계가 없으므로 자기 자신의 프로세스 안에서 작은 uvicorn 서버를 하나 더 띄운다
(`run_scheduled_sync`가 `_serve_trigger_api`를 `asyncio.create_task`로 스케줄러와 같은 이벤트
루프 위에 얹는다).

```bash
uv run knowledge-forge schedule <path> \
  [--trigger-api/--no-trigger-api] [--trigger-host 127.0.0.1] [--trigger-port 8765]

curl -X POST http://127.0.0.1:8765/trigger
```

기본값은 `--trigger-api`(활성화), `127.0.0.1:8765`다. 이 엔드포인트에는 인증이 없으므로
**기본은 localhost 바인딩**이고, 외부에 노출하려면 `--trigger-host 0.0.0.0` 등으로 명시적으로
바꿔야 한다 — 프로세스 종료 시(`finally` 블록) 이 서버 task도 `cancel()`되고 정리된다.

## 커서 연속성 — 왜 첫 tick을 특별 취급하지 않는가

매 tick은 `collector.get_cursor()` → `run_pipeline(..., cursor=cursor)`를 호출한다.
`run_pipeline`은 `cursor`가 있으면 `incremental_sync(cursor)`를 부른다. 그런데 아직 아무
매니페스트도 저장된 적이 없는 **첫 tick**에서도 `get_cursor()`는 빈 매니페스트를 반환하고,
빈 매니페스트를 기준으로 한 `incremental_sync`는 모든 파일을 "새로 생김"으로 판단한다 —
`initial_sync()`를 호출한 것과 결과가 같다. 그래서 스케줄러 코드는 첫 tick과 이후 tick을
분기할 필요가 전혀 없다: 매번 동일하게 "현재 커서 기준 증분 동기화"만 하면 된다
(`LocalFileCollector`의 매니페스트 저장 방식은 [`architecture.md`](architecture.md)의
Collector 절, 구현은 `collectors/local_file.py` 참고).

## tick당 저장소 연결

`store_factory`(0-인자 콜러블, `PipelineStore`를 새로 만들어 반환)가 주어지면, 각 tick이
자신만의 저장소 연결을 열고(`with store_factory() as store:`) tick이 끝나면 닫는다. 스케줄러
전체 수명 동안 커넥션 하나를 계속 들고 있지 않는다 — 수명 관리가 단순해지고, tick 사이에
DB 연결이 끊겨도 다음 tick이 스스로 복구한다.

## 실패 격리

한 tick 안에서 예외가 나면(일시적 DB 장애, Anthropic API 오류 등) 로그로 남기고 그 tick만
건너뛴다 — 스케줄러 프로세스 자체는 죽지 않고 다음 tick이 예정대로 실행된다
(`build_tick`의 `try/except Exception` 참고).

## CLI 옵션은 `extract`와 동일한 값을 공유

`--max-tokens`/`--model`/`--store` 옵션은 `extract` 명령과 정확히 같은 기본값·이름을
쓴다 — 두 명령 사이에 설정을 옮길 때 헷갈리지 않도록 의도적으로 맞췄다. `--interval-seconds`
만 `schedule` 고유 옵션이다.

## `schedule` vs. `serve`/`worker` — 언제 뭘 쓰나

`schedule`은 하나의 장기 실행 프로세스가 스스로 반복 호출하는 **폴링** 모델이다 - 정적 파일
소스처럼 "주기적으로 같은 경로를 증분 동기화"하면 되는 경우에 맞다. 반면 [`api.md`](api.md)의
`serve`/`worker`는 **요청이 오면 즉시 응답하고 처리는 큐 뒤 워커가 넘겨받는** 모델이다 -
사용자가 소스를 그때그때 연결/업로드하는 온디맨드 ingest, 그리고 향후 SaaS 웹훅/event-driven
ingest가 붙을 자리(`roadmap.md` 4단계)에 맞다. 이 문서가 다루는 `schedule`은 file source
전용으로 유지되고, `serve`/`worker`가 SaaS 소스까지 포함하는 general-purpose ingest 경로로
확장된다 - 서로 대체 관계가 아니라 다른 트리거 성격(주기 vs. 요청)에 맞는 별도 실행 모드다.

## 테스트

`run_scheduled_sync`는 이벤트 루프 위에서 영원히 대기하므로 스케줄 루프 자체를 단위
테스트하지 않는다. 대신 `build_tick(...)`이 반환하는 (async) tick 함수를 직접
`await`해서(스케줄러 없이) 검증한다 — `tests/scheduling/test_scheduler.py`가 `tmp_path`에
파일을 만들어 첫 호출(초기 동기화)과 파일 추가 후 두 번째 호출(증분 동기화)의 결과, 그리고
예외가 나도 tick 함수가 예외를 전파하지 않는지를 확인한다.

`trigger_now()`는 실제 `AsyncIOScheduler`를 (이벤트 루프를 영원히 막지 않고) 직접 만들어
검증할 수 있으므로 별도 단위 테스트가 있다 — job을 1시간 뒤로 예약해두고 `trigger_now()` 호출
후 `next_run_time`이 즉시(수 초 이내)로 당겨지는지 확인한다. `DEFAULT_INTERVAL_SECONDS == 600`도
회귀 방지용으로 테스트한다.

`build_trigger_app()`도 마찬가지로 실제 소켓을 열지 않고(`uvicorn.Server` 없이)
`fastapi.testclient.TestClient`로 검증한다 — `GET /health`, 그리고 `POST /trigger`가
`trigger_now()`와 동일하게 job의 `next_run_time`을 즉시로 당기고 그 값을 응답 바디로 돌려주는지
확인한다.
