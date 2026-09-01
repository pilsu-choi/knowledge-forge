# 스케줄링

`knowledge-forge extract`는 1회성 실행이다. 주기적으로 같은 경로를 계속 증분 동기화하고
싶다면 `knowledge-forge schedule`을 쓴다 — 별도 OS cron/systemd timer 없이, 하나의
장기 실행 프로세스 안에서 `APScheduler`(`AsyncIOScheduler`)가 asyncio 이벤트 루프 위의
백그라운드 job으로 반복한다.

```bash
uv run knowledge-forge schedule <path> \
  [--interval-seconds 3600] [--max-tokens 4000] [--model claude-opus-5] [--store/--no-store]
```

포그라운드 프로세스로 실행되며(내부적으로 `asyncio.run(run_scheduled_sync(...))`), Ctrl-C로
정상 종료한다.

## 동작 방식 (`src/knowledge_forge/scheduling/scheduler.py`)

`run_scheduled_sync(root, interval_seconds, max_tokens, model, store_factory=None,
state_path=None)`(async 함수)가:

1. `LocalFileCollector`/`ParserRegistry`/`AdaptiveChunker`/`get_extractor()`(활성
   `KF_LLM_PROVIDER`에 맞는 Extractor, [`llm-providers.md`](llm-providers.md) 참고)를
   **한 번만** 생성해 재사용한다(매 tick마다 다시 만들지 않음).
2. `build_tick(...)`으로 매 tick마다 실행할 **async** 함수를 만든다 — 한 tick 안에서 한 문서의
   여러 청크/표/이미지에 대한 LLM 호출은 `run_pipeline`이 동시 실행한다
   ([`architecture.md`](architecture.md)의 Knowledge Extraction 절 참고).
3. `AsyncIOScheduler.add_job(tick, "interval", seconds=interval_seconds,
   next_run_time=datetime.now())`로 등록 — `next_run_time=datetime.now()`가 시작 즉시 1회
   실행 후 이후 `interval_seconds`마다 반복되게 한다. `scheduler.start()`는 즉시 반환하고,
   `run_scheduled_sync`는 `await asyncio.Event().wait()`로 이벤트 루프를 계속 돌려 tick들이
   백그라운드에서 실행되게 한다(Ctrl-C가 `KeyboardInterrupt`로 이 대기를 깨운다).

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

## 테스트

`run_scheduled_sync`는 이벤트 루프 위에서 영원히 대기하므로 스케줄 루프 자체를 단위
테스트하지 않는다. 대신 `build_tick(...)`이 반환하는 (async) tick 함수를 직접
`await`해서(스케줄러 없이) 검증한다 — `tests/scheduling/test_scheduler.py`가 `tmp_path`에
파일을 만들어 첫 호출(초기 동기화)과 파일 추가 후 두 번째 호출(증분 동기화)의 결과, 그리고
예외가 나도 tick 함수가 예외를 전파하지 않는지를 확인한다.
