# Search Evaluation / Feedback Loop + Groundedness Gate (설계 문서)

`search-architecture.md`가 "Search Evaluation / Feedback Loop — 설계만, 미구현"과
"Iterative / Agentic Retrieval — 이미 구현됨(암묵적 판단에 의존)"으로 남겨둔 두 항목을 구체적인
설계로 채운다. 이 저장소에는 지금 쿼리/답변/근거를 기록하는 테이블이 하나도 없고, "검색이 잘
됐는가"를 코드로 판단할 방법도 없다 — 아래는 그 공백을 메우는 네 조각의 설계이며, 서로
독립적으로 구현 가능하지만 (A)가 나머지 전부의 전제조건이다.

## 스코프: 독립적인 네 조각

| 조각 | 내용 | 골드셋 필요 여부 | 우선순위 |
|---|---|---|---|
| (A) 로깅 인프라 | query/answer/evidence를 Postgres에 기록 | 불필요 | 1 — 전제조건 |
| (B) Groundedness/Citation 자동 평가 | LLM-as-judge로 claim-evidence 매칭, 비동기 배치 | 불필요 | 2 — 바로 가치 |
| (C) Recall/Precision/MRR/NDCG | relevance judgment 필요 | **필요** | 3 — 사람 큐레이션 선행 |
| (D) 명시적 Groundedness Gate | (B)의 판정기를 온라인 게이트로 재사용, iterative retrieval 강화 | 불필요 | 4 — (B) 데이터로 필요성 확인 후 |

## (A) 로깅 인프라 — `search_log` / `search_feedback`

```sql
-- 이벤트 로그라 knowledge_objects류와 달리 PK가 stable_id가 아니라 uuid4
-- (jobs 테이블과 같은 이유 - storage/schema.sql 참고).
CREATE TABLE IF NOT EXISTS search_log (
    log_id        TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL DEFAULT 'default',
    query         TEXT NOT NULL,
    answer        TEXT NOT NULL,
    -- 실제 검색된 근거: [{source, path|knowledge_object_id, rank, score}, ...]
    -- - eval_score.unsupported_claims가 이 목록의 원소를 가리킨다.
    evidence      JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- 실행된 도구 호출 시퀀스: [{tool, arguments, iteration}, ...]
    -- - 반복 횟수/재검색 여부를 나중에 여기서 재구성한다(별도 카운터 컬럼 불필요).
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

**어디서 기록하는가**: `agent/loop.py::run_agent`는 건드리지 않는다 — 이 함수는 벤더/모드
중립 공유 루프라는 게 `agent-runtime.md`가 세운 원칙이고, 로깅처럼 그 루프의 책임이 아닌 걸
안에 넣으면 원칙이 깨진다. 대신 `run_agent`가 이미 반환하는 `(answer, messages)`의 `messages`
안에 모든 tool call/tool result가 들어있으므로, 호출부(`agent/runtime.py::run_query`,
`AgentSession.ask`)에서 턴이 끝난 뒤 `messages`를 파싱해 `search_log` 한 행으로 남긴다 —
`run_agent` 자체는 로깅의 존재를 전혀 모른다.

**피드백 캡처 지점**:
- `--interactive` REPL: `AgentSession`이 직전 턴의 `log_id`를 들고 있으니 `/feedback up|down
  [comment]` 커맨드를 추가해 그 자리에서 붙인다.
- 일회성 `run_query`: 프로세스가 답변 출력 후 바로 끝나 나중에 피드백을 붙일 방법이 필요하다 —
  `run_query`가 `log_id`를 stdout에 같이 찍고(`--json` 출력 모드), 별도 `knowledge-forge
  feedback <log_id> up|down [--comment ...]` CLI 커맨드로 사후에 붙이는 방식을 제안한다.
- HTTP 경로는 스코프 밖 — `api.md`에는 아직 채팅/검색 엔드포인트 자체가 없다(`agent-search.md`가
  `POST /search/vector`를 설계만으로 남겨둔 것과 동일한 이유). 그게 생기면 그 엔드포인트 응답에
  `log_id`를 얹고 `POST /search/feedback`을 추가하는 확장이 된다.

## (B) Answer Groundedness / Citation Accuracy — LLM-as-judge, 골드셋 불필요

지금 당장 시작 가능한 조각 — "정답"이 있어야 하는 (C)와 달리, judge는 로그에 이미 있는
`(query, answer, evidence)`만으로 채점한다.

```text
search_log (미채점 행들)
      │
      ▼
1) Claim Extraction  ── LLM 호출: 답변 문장들을 개별 주장(claim) 목록으로 분해
      │
      ▼
2) Evidence Matching  ── LLM 호출: 각 claim이 evidence 중 어느 것으로
      │                   지지되는지 판정 — 지지됨 / 미지지 / 모순, 3-way
      ▼
3) 점수 집계
      groundedness_score   = 지지된 claim 수 / 전체 claim 수
      citation_precision   = (인용된 path/knowledge_object_id 중 evidence에
                               실제로 존재하고, 그 claim을 실제로 지지하는 것) / 전체 인용 수
      │
      ▼
eval_score 테이블에 저장 (unsupported_claims에 미지지 claim 원문 + 이유 기록
 → 사람이 나중에 "왜 이 질의가 실패했는지" 바로 읽을 수 있게)
```

```sql
CREATE TABLE IF NOT EXISTS eval_score (
    log_id              TEXT PRIMARY KEY REFERENCES search_log(log_id),
    groundedness_score  DOUBLE PRECISION NOT NULL,
    citation_precision  DOUBLE PRECISION,
    unsupported_claims  JSONB NOT NULL DEFAULT '[]'::jsonb,
    judge_model         TEXT NOT NULL,
    judged_at           TIMESTAMPTZ NOT NULL
);
```

**실행 방식**: `embeddings.md`의 `--embed`/`knowledge-forge embed`와 같은 패턴 — 별도
동기 경로에 끼워 넣지 않고, 아직 채점 안 된 `search_log` 행을 가져와 배치로 도는 `knowledge-forge
eval` CLI/스케줄 잡. 답변 지연시간에 영향을 주지 않는다.

**한계**: judge도 LLM이라 오판할 수 있다 — 이건 golden truth가 아니라 프로덕션에서 흔히 쓰는
저비용 근사치(RAG 평가에서 표준적인 완화책)다. `unsupported_claims`를 사람이 주기적으로
샘플링해 judge 정확도 자체를 검증하는 루틴이 있어야 신뢰도를 유지한다.

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

## (D) 명시적 Groundedness Gate — Iterative Retrieval 강화, 옵션

`search-architecture.md`는 "한 번 검색하고 끝내지 않는다"는 요구사항이 `agent/loop.py`의
tool-calling 루프로 이미 충족된다고 판단했다 — 다만 "근거가 충분한가"는 전적으로 모델의 암묵적
자기평가에 의존하고, 강제되는 검증 단계는 없다. (B)에서 만든 claim-evidence 판정기를 그대로
재사용하면, 답변을 확정하기 직전에 한 번 더 검증하는 게이트를 추가할 수 있다.

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

## 우선순위

1. **(A) 로깅 인프라** — 나머지 전부의 전제조건, 다른 조각 없이도 그 자체로 "질의별 실패 원인
   추적"의 최소 형태(사람이 `search_log`를 직접 읽는 것)를 제공한다.
2. **(B) Groundedness/Citation LLM-judge 배치** — 골드셋 없이 바로 가치, 실패 유형 데이터가
   쌓이기 시작한다.
3. **(C) 골드셋 기반 Recall/Precision/MRR/NDCG** — 회귀 테스트로 먼저 도입, 실트래픽 근사는
   그 다음.
4. **(D) 온라인 Groundedness Gate** — (B) 데이터로 실제 필요성이 확인된 뒤 켜는 옵션.

## 스코프 밖

- **대시보드/시각화** — 이 문서는 데이터 모델과 수집·채점까지다. 집계 뷰가 필요해지면
  `ui.md`의 확장 과제.
- **A/B 테스트 인프라**(reranking 전략 비교 등) — `eval_score`가 쌓이면 자연스러운 다음 단계지만
  지금 범위 밖.
- **Graph/SQL search가 라우팅에 추가될 때의 스키마 변경** — `evidence`/`tool_calls`가 이미
  JSONB라 소스 종류가 늘어도 컬럼 추가 없이 흡수된다.
