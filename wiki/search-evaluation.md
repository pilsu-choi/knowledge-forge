# Search Evaluation / Feedback Loop + Groundedness Gate

`search-architecture.md`가 "Search Evaluation / Feedback Loop — 설계만, 미구현"과
"Iterative / Agentic Retrieval — 이미 구현됨(암묵적 판단에 의존)"으로 남겨둔 두 항목을 채우는
네 조각 - (A)/(B)는 구현됨, (C)/(D)는 여전히 설계만이다. (A)가 나머지 전부의 전제조건이다.

## 스코프: 독립적인 네 조각

| 조각 | 내용 | 골드셋 필요 여부 | 상태 |
|---|---|---|---|
| (A) 로깅 인프라 | query/answer/evidence를 Postgres에 기록 | 불필요 | **구현됨** |
| (B) Groundedness/Citation 자동 평가 | LLM-as-judge로 claim-evidence 매칭, 비동기 배치 | 불필요 | **구현됨** |
| (C) Recall/Precision/MRR/NDCG | relevance judgment 필요 | **필요** | 설계만, 미구현 |
| (D) 명시적 Groundedness Gate | (B)의 판정기를 온라인 게이트로 재사용, iterative retrieval 강화 | 불필요 | 설계만, 미구현 |

## (A) 로깅 인프라 — **구현됨**, `storage/eval_store.py::EvalStore`

`search_log`/`search_feedback` 테이블(`storage/schema.sql`) - `JobStore`와 같은 이유로
`PostgresStore`와 별도 클래스(`EvalStore`)를 뒀다: 저 둘은 Knowledge Graph 콘텐츠를 다루지만
이건 이벤트 로그이고 호출자/lifecycle이 다르다. PK가 `knowledge_objects`류와 달리 stable_id가
아니라 uuid4인 것도 `jobs.job_id`와 같은 이유(이벤트라 재실행 idempotency가 의미 없음).

```sql
CREATE TABLE IF NOT EXISTS search_log (
    log_id        TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL DEFAULT 'default',
    query         TEXT NOT NULL,
    answer        TEXT NOT NULL,
    -- [{tool, iteration, payload}, ...] - 최초 설계는 {source, path|
    -- knowledge_object_id, rank, score}로 정규화하려 했지만, sql_search의
    -- {columns, rows}/plan_search의 {subqueries, evidence}가 file_search/
    -- vector_search의 hit 모양과 아예 다르다는 걸 구현 중에 발견 - 강제로
    -- 맞추는 대신 각 도구의 원본 반환 모양을 그대로 담는다. (B)의 judge가
    -- 이걸 고정 스키마가 아니라 자유 형식 JSON 컨텍스트로 읽으므로 문제 없다.
    evidence      JSONB NOT NULL DEFAULT '[]'::jsonb,
    tool_calls    JSONB NOT NULL DEFAULT '[]'::jsonb,
    latency_ms    INT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_log_project_id ON search_log(project_id);
CREATE INDEX IF NOT EXISTS idx_search_log_created_at ON search_log(created_at);

CREATE TABLE IF NOT EXISTS search_feedback (
    feedback_id  TEXT PRIMARY KEY,
    log_id       TEXT NOT NULL REFERENCES search_log(log_id),
    rating       TEXT NOT NULL,  -- 'up' | 'down'
    comment      TEXT,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_feedback_log_id ON search_feedback(log_id);
```

**`evidence`/`tool_calls`를 어떻게 재구성하는가**: `agent/loop.py::run_agent`는 여전히
로깅을 전혀 모른다 - 대신 `_wrap_untrusted`(도구 결과를 `<tool_result tool="...">...
</tool_result>`로 감싸는 함수)의 역변환인 `parse_tool_result()`를 같은 파일에 추가했다.
`evaluation/search_log.py::extract_evidence`/`extract_tool_calls`가 턴이 끝난 뒤
`messages`를 순회하며 이걸로 각 tool_result를 다시 구조화된 값으로 복원한다
(`file_search`/`vector_search`/`sql_search`/`plan_search`만 "evidence"로 취급 -
`write_memory`/`read_file` 같은 부수효과 도구는 제외). 호출부는
`evaluation/search_log.py::save_search_log()`이고, `agent/runtime.py::run_query`/
`AgentSession.ask`가 턴이 끝날 때마다 이걸 호출한다 - `KF_DATABASE_URL`이 없으면
아무 것도 하지 않고(`vector_search`/`sql_search`와 같은 "설정 안 되면 조용히 빠짐" 컨벤션),
로깅 자체가 실패해도(DB 다운, 스키마 미적용) 예외를 삼키고 경고만 남긴다 - 이미 만들어진
답변을 로깅 실패로 날릴 수는 없다.

**API 변화**: `run_query()`가 이제 `answer` 대신 `(answer, log_id)`를 반환한다(`log_id`는
로깅이 꺼져있거나 실패하면 `None`). `AgentSession`은 `session.plan`과 같은 패턴으로
`session.last_log_id` 프로퍼티를 노출한다.

**피드백 캡처 지점** (설계 문서가 제안한 그대로 구현):
- `--interactive` REPL: `/feedback up|down [코멘트]` - `session.last_log_id`에 붙인다
  (`cli.py::_handle_feedback_command`).
- 일회성 `knowledge-forge agent <질문>`: 답변과 함께 `[log_id=... - knowledge-forge feedback
  <log_id> up|down로 평가 가능]`을 stderr에 출력하고, 신규 `knowledge-forge feedback <log_id>
  up|down [--comment ...]` 커맨드로 사후에 붙인다.
- HTTP 경로는 여전히 스코프 밖 - `api.md`에 채팅/검색 엔드포인트 자체가 아직 없다.

## (B) Answer Groundedness / Citation Accuracy — **구현됨**, `evaluation/judge.py`

`(query, answer, evidence)`만으로 채점한다 - "정답"이 있어야 하는 (C)와 달리 골드셋이 필요
없다.

```text
search_log (미채점 행들, EvalStore.list_unscored_search_logs)
      │
      ▼
judge_search_log() - ChatProvider.send() 1회 호출
      │  (claim extraction과 evidence matching을 한 프롬프트/한 호출로 처리 -
      │   최초 설계는 이 둘을 별도 LLM 호출 2개로 나눴지만, 구현하면서 한
      │   구조화 출력 호출로 합쳤다: 모델이 claim과 evidence를 같은 컨텍스트에서
      │   보게 되고, 배치로 여러 로그를 돌 때 라운드트립이 절반으로 준다)
      ▼
{"claims": [{text, verdict: supported|unsupported|contradicted, reason, citation_valid}]}
      │
      ▼
groundedness_score = 지지된 claim 수 / 전체 claim 수 (claim이 하나도 없으면 1.0 -
                      "근거를 찾지 못했습니다"류 답변은 애초에 뒷받침할 게 없으므로)
citation_precision  = citation_valid가 non-null인 claim 중 true 비율 (아무 claim도
                      인용을 달지 않았으면 None)
      │
      ▼
eval_score.claims에 전체 claim 저장(미지지 것만이 아니라 전부 - 사람이 나중에
judge 정확도 자체를 샘플링 검증할 수 있게)
```

```sql
CREATE TABLE IF NOT EXISTS eval_score (
    log_id              TEXT PRIMARY KEY REFERENCES search_log(log_id),
    groundedness_score  DOUBLE PRECISION NOT NULL,
    citation_precision  DOUBLE PRECISION,
    claims              JSONB NOT NULL DEFAULT '[]'::jsonb,
    judge_model         TEXT NOT NULL,
    judged_at           TIMESTAMPTZ NOT NULL
);
```

**실행 방식**: `embeddings.md`의 `--embed`/`knowledge-forge embed`와 같은 패턴 - 신규
`knowledge-forge eval [--project-id] [--batch-size] [--max-batches] [--provider] [--model]`
이 `EvalStore.list_unscored_search_logs()`로 미채점 행을 배치로 가져와 채점하고
`eval_score`에 저장한다. 답변 생성 경로와 완전히 분리된 오프라인 배치라 지연시간에 영향이
없다.

**실패 처리**: `judge_search_log()`는 provider 호출이 실패하거나(네트워크 에러) 응답이
파싱 불가능한 JSON이면 점수를 저장하지 않고 `None`을 반환한다 - `eval` 커맨드는 그 로그를
그냥 건너뛰고(허위 점수를 저장하지 않음), `list_unscored_search_logs()`가 여전히 그 로그를
미채점으로 보므로 다음 `eval` 실행에서 자동으로 재시도된다. 반대로 judge가 정상적으로
`{"claims": []}`를 반환한 경우(진짜로 채점할 claim이 없는 답변)는 실패가 아니라
groundedness_score=1.0으로 정상 채점된다 - 이 둘을 구분하지 않으면 네트워크 장애가 조용히
"완벽하게 grounded"로 잘못 기록될 뻔했다.

**한계**: judge도 LLM이라 오판할 수 있다 - 이건 golden truth가 아니라 프로덕션에서 흔히 쓰는
저비용 근사치(RAG 평가에서 표준적인 완화책)다. `eval_score.claims`(실패한 것만이 아니라 전부)를
사람이 주기적으로 샘플링해 judge 정확도 자체를 검증하는 루틴이 있어야 신뢰도를 유지한다.

## (C) Recall / Precision / MRR / NDCG — relevance judgment 필요, 별도 트랙

**왜 로그만으로 계산 불가능한가**: 이 지표들은 "이 질의에 어떤 문서가 관련 있는가"라는 정답이
있어야 정의된다. 실 트래픽 로그에는 이 정답이 없다 — 사용자가 본 적 없는 관련 문서를 놓친
경우(false negative)가 로그에 아예 나타나지 않으므로, 검색이 놓친 것과 애초에 관련 문서가
없던 것을 로그만으로 구분할 수 없다.

**제안 경로**:
1. **소규모 큐레이션 회귀 셋** — 대표 질의 30~50개 + 사람이 미리 표시한 relevant
   `knowledge_object_id` 목록을 fixture로 고정(예: `tests/eval/golden_queries.json`).
   `search/rerank.py`(RRF 파라미터)나 `search/router.py`(라우팅 휴리스틱), 임베딩 모델 교체처럼
   검색 품질에 영향을 주는 변경마다 이 셋으로 Recall@k/MRR/NDCG를 계산해 회귀를 잡는다 — CI성
   오프라인 평가, 실사용 트래픽과 무관하게 언제든 돌릴 수 있다.
2. **실 트래픽 근사(보조 신호)** — `search_feedback.rating = 'up'`이 달린 로그의 `evidence`를
   "관련 있음"으로 잠정 채택해 골드셋을 점진적으로 불린다. 노이즈가 크다(사용자는 최종 답변만
   보고 평가하지, 근거 하나하나의 관련성을 판단하지 않는다) — 트래픽이 충분히 쌓인 뒤의 보조
   신호로만 쓰고, 1)의 큐레이션 셋을 대체하지 않는다.

사람 노동(큐레이션)이 선행돼야 해서 (A)/(B)보다 뒤 순위 — 하지만 (A)가 로그 스키마를 이미
`evidence` JSONB로 잡아두므로, (C)를 시작할 때 스키마 변경은 필요 없다.

**채점 단위는 `document_id`** — `knowledge_object_embeddings`가 KO grain과 청크 grain
(`KF_EMBEDDING_SOURCE`, [`embeddings.md`](embeddings.md)) 두 모드를 담게 되면서, 골든셋의 정답을
`knowledge_object_id`로만 잡으면 청크 모드 검색은 채점할 수 없다(히트에 KO id가 없다). 두 grain이
공유하는 `document_id`를 정답의 기본 단위로 두고, 모드가 지원할 때만 `chunk_id`/`knowledge_object_id`로
엄격 채점을 덧붙이는 게 맞다 — 그래야 "KO 임베딩 vs 청크 임베딩" 자체를 같은 골든셋으로 A/B할 수
있다. `vector_search(target_kind=...)`가 한 grain만 따로 재기 위한 파라미터다. 골든셋 자동 생성은
청크 원문(`chunks.text`)에서 질의를 만들면 추출 품질 상한도 피한다 — 정답 저장 모양은
`{chunk_id, document_id, knowledge_object_ids: [...]}`.

## (D) 명시적 Groundedness Gate — Iterative Retrieval 강화, 옵션

`search-architecture.md`는 "한 번 검색하고 끝내지 않는다"는 요구사항이 `agent/loop.py`의
tool-calling 루프로 이미 충족된다고 판단했다 — 다만 "근거가 충분한가"는 전적으로 모델의 암묵적
자기평가에 의존하고, 강제되는 검증 단계는 없다. (B)에서 이미 구현한
`evaluation/judge.py::judge_search_log`를 그대로 재사용하면, 답변을 확정하기 직전에 한 번 더
검증하는 게이트를 추가할 수 있다 - 이 조각 자체는 아직 미구현이다.

```text
run_agent 루프: tool_calls 없는 turn 도착 (= 최종 답변 후보)
      │
      ▼
게이트 활성화? (KF_AGENT_GROUNDEDNESS_GATE_ENABLED, 기본 false)
      │ NO ──────────────────────────────────► 그대로 반환 (현재 동작)
      │ YES
      ▼
claim-evidence 판정을 즉석에서 1회 실행 (동기, (B)의 배치가 아닌 실시간 호출)
      │
      ├─ 전부 지지됨 ──────────────────────────► 그대로 반환
      │
      └─ 미지지 claim 있음
              │
              ▼
        "이 주장(들)은 지금까지의 근거로 뒷받침되지 않는다" 합성 tool_result를
        messages에 추가(_wrap_untrusted 형식) → 루프 계속, 모델이 재검색하거나
        해당 주장을 답변에서 빼도록 유도
              │
              ▼
        (KF_AGENT_GROUNDEDNESS_GATE_MAX_RETRIES회 반복해도 미지지 claim이 남으면)
        "근거 부족"을 답변에 명시하도록 강제하고 종료 — 무한루프 방지
```

**설정 제안** (`config.py`의 기존 `agent_max_iterations`/`agent_context_compaction_*`와 같은
네이밍 규약):

```python
agent_groundedness_gate_enabled: bool = False
agent_groundedness_gate_max_retries: int = 2
```

**왜 기본값이 꺼짐(false)인가**: 매 최종 답변마다 judge 호출이 1회 이상 추가되어 지연시간/비용이
늘어난다 — 지금 규모에서 이게 필요한지는 (B)의 오프라인 배치로 실사용 hallucination/citation
오류 빈도를 먼저 관찰한 뒤 판단하는 게 맞다. 데이터 없이 미리 켜두면 근거 없는 비용 증가가 된다.

## 상태와 다음 순서

1. **(A) 로깅 인프라 — 구현됨.** 나머지 전부의 전제조건, 다른 조각 없이도 그 자체로 "질의별
   실패 원인 추적"의 최소 형태(사람이 `search_log`를 직접 읽는 것)를 제공한다.
2. **(B) Groundedness/Citation LLM-judge 배치 — 구현됨.** `knowledge-forge eval`을 주기적으로
   돌리기 시작하면 실패 유형 데이터가 쌓인다.
3. **(C) 골드셋 기반 Recall/Precision/MRR/NDCG — 설계만.** 회귀 테스트로 먼저 도입, 실트래픽
   근사는 그 다음 - 사람 큐레이션(대표 질의 30~50개 + relevant id 목록)이 선행돼야 한다.
4. **(D) 온라인 Groundedness Gate — 설계만.** `knowledge-forge eval`을 몇 주 돌려 실사용
   hallucination/citation 오류 빈도가 실제로 문제 될 만큼 나오는지 (B)의 데이터로 먼저
   확인한 뒤 켜는 옵션 - 데이터 없이 미리 만들면 근거 없는 지연시간/비용 증가가 된다.

## 스코프 밖

- **대시보드/시각화** — 이 문서는 데이터 모델과 수집·채점까지다. 집계 뷰가 필요해지면
  `ui.md`의 확장 과제.
- **A/B 테스트 인프라**(reranking 전략 비교 등) — `eval_score`가 쌓이면 자연스러운 다음 단계지만
  지금 범위 밖.
- **Graph/SQL search가 라우팅에 추가될 때의 스키마 변경** — `evidence`/`tool_calls`가 이미
  JSONB라 소스 종류가 늘어도 컬럼 추가 없이 흡수된다.
