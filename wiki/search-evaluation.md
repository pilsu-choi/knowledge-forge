# Search Evaluation / Feedback Loop + Groundedness Gate

`search-architecture.md`가 "Search Evaluation / Feedback Loop — 설계만, 미구현"과
"Iterative / Agentic Retrieval — 이미 구현됨(암묵적 판단에 의존)"으로 남겨둔 두 항목을 채우는
네 조각 - (A)/(B)는 구현됨, (C)는 골든셋·지표 모듈까지 구현되고 CLI 배선이 남았으며, (D)는 여전히 설계만이다. (A)가 나머지 전부의 전제조건이다.

## 스코프: 독립적인 네 조각

| 조각 | 내용 | 골드셋 필요 여부 | 상태 |
|---|---|---|---|
| (A) 로깅 인프라 | query/answer/evidence를 Postgres에 기록 | 불필요 | **구현됨** |
| (B) Groundedness/Citation 자동 평가 | LLM-as-judge로 claim-evidence 매칭, 비동기 배치 | 불필요 | **구현됨** |
| (C) Recall/Precision/MRR/NDCG | relevance judgment 필요 | **필요** | 골든셋 스키마·v0·지표 모듈 **구현됨**, `eval-ir` CLI 미구현 |
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

## (C) Recall / Precision / MRR / NDCG — 골든셋 스키마·지표 모듈 구현됨, `eval-ir` CLI는 다음 단계

**왜 로그만으로 계산 불가능한가**: 이 지표들은 "이 질의에 어떤 문서가 관련 있는가"라는 정답이
있어야 정의된다. 실 트래픽 로그에는 이 정답이 없다 — 사용자가 본 적 없는 관련 문서를 놓친
경우(false negative)가 로그에 아예 나타나지 않으므로, 검색이 놓친 것과 애초에 관련 문서가
없던 것을 로그만으로 구분할 수 없다. 그래서 정답(골든셋)을 별도 파일로 고정하고, 그 파일로
검색 결과를 채점한다.

### 구현된 조각

| 파일 | 역할 |
|---|---|
| `evaluation/golden.py` | 골든셋 pydantic 모델(`GoldenSet`/`GoldenQuery`/`GoldenExpected`), `QUERY_TYPES`, `load_golden`/`save_golden`, `compute_source_hash`, `check_staleness` |
| `evaluation/ir_metrics.py` | 순수 함수 지표(DB/LLM/numpy 없음): `recall_at_k`/`precision_at_k`/`hit_at_k`/`mrr`/`ndcg_at_k`/`exhaustive_recall`/`count_match`/`none_correct`, 유형별 묶음 `score_query`, 전체·유형별 평균 `aggregate`, A/B용 `paired_delta`/`bootstrap_ci` |
| `tests/eval/golden/v0.json` | 손으로 쓴 v0 골든셋(76개, 아래 참고) |
| `tests/eval/golden/validate_v0.py` | 실 DB 대조 검증 스크립트(id 존재·소속, `source_hash` 일치) — DB가 필요하므로 pytest 밖 |
| `tests/evaluation/test_golden.py`, `test_ir_metrics.py` | 로더 불변식·지표 손계산·집계·bootstrap 결정론 (DB 불필요, v0.json 로딩 포함) |

### 골든셋 스키마

```json
{
  "golden_version": "v0",
  "created_at": "2026-09-04T00:00:00Z",
  "project_id": null,
  "index_snapshot": {"documents": 263, "chunks": 936, "knowledge_objects": 5107,
                     "sources_by_type": {"gmail": 59, "google_drive": 246, "slack": 197}},
  "queries": [
    {
      "id": "v0-factoid-002",
      "query": "MLOps 개발계 보안취약점 패치 작업의 전체 예상 소요 시간은?",
      "primary_type": "factoid",
      "types": ["factoid"],
      "expected": {"document_ids": ["51a30b62..."], "exhaustive": false, "count": null, "none": false,
                   "chunk_ids": ["9b34d176..."], "knowledge_object_ids": ["9e091eb8..."]},
      "origin": "manual",
      "notes": "정답: 3시간 이상(MinIO 패치가 데이터 양에 비례).",
      "source_hash": "sha256(...)",
      "as_of": null
    }
  ]
}
```

- **유형은 multi-label** (`types`) + 하나의 `primary_type`. "최근 세아제강 메일"은
  relative_temporal이면서 entity_centric이다 — 단일 라벨을 강제하면 유형별 집계가 거짓말을 한다.
  `primary_type`은 단일 라벨 뷰가 필요할 때(유형별 개수 표 등) 정확히 한 버킷에 넣기 위한 것.
- **채점 단위는 `document_id`** — KO grain과 청크 grain(`KF_EMBEDDING_SOURCE`,
  [`embeddings.md`](embeddings.md))이 공유하는 유일한 키라서, KO id로 정답을 잡으면 청크 모드는
  채점이 불가능하다. `chunk_ids`/`knowledge_object_ids`는 모드가 그 id를 노출할 때만 덧붙이는
  엄격 채점(`score_query(..., ranked_chunk_ids=)` → `chunk_hit@k`/`chunk_mrr`).
- **정답 모양은 유형마다 다르다** — `exhaustive=true`는 "이 목록이 관련 문서 전부"(@k가 아니라
  전체 결과 대비 recall), `count`는 집계 정답, `none=true`는 "관련 문서 없음"이 정답.
  로더가 유형별 불변식을 거부한다: null/negative ↔ `none=true`, aggregation → `count` 필수,
  set_query → `exhaustive` 필수, relative_temporal/latest → `as_of` 필수, ambiguous → 정답 2개 이상,
  `none=true`면 id 목록 비어야 함, `exhaustive`면 id ≥ 1.
- **`as_of`**: 상대 날짜("지난주", "가장 최근")는 평가 시점에 따라 답이 바뀌므로 기준 시점을
  항목에 박는다. `eval-ir`는 검색 호출 시 이 값을 "지금"으로 넘겨야 한다.
- **`source_hash`**: 정답 문서들의 `sources.content_hash`를 정렬·중복 제거해 이어 sha256한 값.
  파일을 고치지 않고도 `check_staleness(golden, {document_id: content_hash})`로 인덱스 드리프트를
  잡는다(`missing`/`changed`/`unhashed`). 순수 함수라 DB 연결은 호출자(CLI)가 갖는다.
- **버전 고정**: `v0.json`은 불변. 수정은 새 버전 파일로 — 같은 파일을 제자리에서 고치면 과거
  eval 결과와 비교가 조용히 깨진다. `project_id: null`은 "인덱스의 모든 프로젝트"(개발 DB에
  13개 project_id가 섞여 있고 `search.service`도 None을 전체로 취급).

### 유형 목록과 유형별 채점 방식

`score_query`가 `expected`의 모양을 보고 필요한 지표만 묶는다(유형 이름을 직접 보지 않는다 —
같은 유형이라도 정답 모양이 다를 수 있어서). 기본 `k_values=(1, 3, 5, 10)`.

| 유형 | 정답 모양 | 주 지표 | 비고 |
|---|---|---|---|
| factoid | 문서 1 + `chunk_ids` | MRR, hit@k, (chunk_mrr) | 청크 텍스트로만 답할 수 있는 질문 |
| navigational | 문서 1 | MRR, hit@1 | gdrive `name` 변형(확장자 제거·줄임) |
| whole_document_summary | 문서 1 | hit@k, recall@k | 문서 단위 요약 |
| specific_document_qa | 문서 1 (+chunk) | MRR, hit@k | 문서명 + 절 질문 |
| temporal | 문서 n, 대개 exhaustive | recall@k, exhaustive_recall | 절대 날짜, `sources.modified_at` 기준 |
| relative_temporal | 문서 n + `as_of` | recall@k, exhaustive_recall | "최근 3일", "이번 주" |
| metadata_filter | 문서 n, exhaustive | exhaustive_recall/precision | 발신 도메인, mimeType, subject 접두 |
| exact_match | 문서 1~n | MRR, hit@1 | 파일 id, repo URL, 호스트명 |
| entity_centric | 문서 n, exhaustive | exhaustive_recall | 조직·프로젝트명(개인 이름 금지) |
| multihop | 문서 정확히 2 | recall@k, exhaustive_complete | 두 문서 모두 있어야 정답 |
| cross_document_comparison | 문서 정확히 2 | recall@k, exhaustive_complete | 비교 대상 둘 다 필요 |
| aggregation | `count` (+문서 목록) | count_match (+exhaustive_recall) | `notes`에 센 SQL 기록 |
| set_query | 문서 n, exhaustive | exhaustive_recall/precision/complete | 전체 목록 |
| negative | `none: true` | none_correct | 언급된 적 없는 기술 |
| null | `none: true` | none_correct | DB에 없는 주제 |
| latest | 문서 1 + `as_of` | hit@1, MRR | 같은 계열 중 가장 최근 |
| version | 문서 1~n | MRR, exhaustive_recall | 같은 공지/메일의 다른 버전 |
| parent_child | 문서 1 + `chunk_ids` | hit@k, chunk_hit@k | 문서(부모) 안의 절(자식 청크) |
| conversation_thread | 문서 1 | hit@k, MRR | slack `thread_ts`로 묶인 스레드 문서 |
| sequence | 문서 n, exhaustive | exhaustive_recall | 시간순 연쇄 (순서 자체는 채점 안 함) |
| long_range_context | 문서 1 (긴 문서) | hit@k, recall@k | 100+ 청크 문서 전체 맥락 |
| ambiguous | 문서 ≥2, exhaustive | exhaustive_recall | 한 이름에 여러 문서 |

`none_correct`는 결과가 비었으면 정답, 호출자가 `scores`+`threshold`를 주면 최고 점수가 임계
이하일 때도 정답 — 임계는 retriever의 점수 스케일에 따라 다르므로 기본값이 없다.
`aggregate(per_query, types_by_id)`는 전체/유형별 평균과 `n`을 내는데, multi-label이라 유형별
`n`의 합은 전체 `n`보다 크고, 지표별 평균은 그 지표를 가진 질의 위에서만 계산된다(`metric_n`).

### v0의 성격 — 손 작성·미검수·개발 DB 기준

`tests/eval/golden/v0.json`은 개발 DB(`documents 263 / chunks 936 / knowledge_objects 5107`,
2026-09-04)를 직접 조회해 **정답 document_id가 실제로 존재하는** 질의만 76개 손으로 썼다.
사람 검수를 거치지 않은 스켈레톤이라 절대값 보고에 쓰기엔 이르고, 스키마·지표·CLI 배선을
검증하는 회귀용이다. 개발 DB의 제약이 그대로 반영돼 있다:

- gdrive 소스 246개 중 documents가 있는 건 7개(마크다운 6 + pptx 1)뿐이라 navigational/
  whole_document_summary/ambiguous가 전부 그 7개 위에서 돈다.
- `relations`에 문서 간 edge가 하나도 없어(6,993건 모두 문서 내부) multihop은 같은 엔티티를
  공유하는 두 문서의 KO 쌍으로 구성했다(`notes`에 hop 경로 기록).
- `chunks.parent_chunk_id`가 전부 NULL이라 parent_child는 "문서 안의 절 청크"로 해석했다.
- 개인 이름은 질의에 넣지 않았다(역할·조직·채널·프로젝트명만) — 파일이 리포에 커밋되기 때문.

유형별 개수(primary_type 기준): factoid 10, navigational 4, specific_document_qa 4,
metadata_filter 4, 나머지 18개 유형 각 3 (negative/null 각 3 = "정답 없음" 6개, ~8%).

### 다음 단계

1. **`knowledge-forge eval-ir`** — `load_golden`으로 파일을 읽고 질의마다 `search/retrieve.py`
   (또는 `search/service.py`, 에이전트 루프 없이 temperature 0)를 호출해 document_id 순서를
   얻은 뒤 `score_query` → `aggregate`. 결과는 `eval_run`(golden_version, config_hash,
   index_snapshot, per_query)로 기록. 실행 전 `check_staleness`로 드리프트를 경고.
   `--compare <runA> <runB>`는 `paired_delta` + `bootstrap_ci`로 질의별 win/loss/tie와 CI를 낸다 —
   같은 golden_version + 같은 index_snapshot인 두 run만 비교.
2. **`knowledge-forge eval-gen`** — `chunks.text`에서 층화 샘플링(type/document_type/source) →
   LLM 질의 생성 → 자동 필터 → `origin: "synthetic"`으로 **v1 발행**. v0는 그대로 두고 v1이
   사람 검수(Tier A)를 거친 첫 절대값 기준이 된다.
3. **`eval-gen --from-traffic`** — `search_feedback.rating='up' ∧ groundedness ≥ 0.9`인
   `search_log`의 evidence를 `origin: "feedback"`으로 편입(Tier C). 노이즈가 크므로 큐레이션 셋을
   대체하지 않는다.

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
3. **(C) 골드셋 기반 Recall/Precision/MRR/NDCG — 스키마·v0·지표 모듈 구현됨, CLI 다음.**
   `evaluation/golden.py`/`ir_metrics.py`와 `tests/eval/golden/v0.json`(손 작성 76개, 미검수)까지
   있고, `eval-ir`가 `search/retrieve.py`를 호출해 이 파일로 채점하는 배선과 `eval-gen`의 v1
   발행이 남았다.
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
