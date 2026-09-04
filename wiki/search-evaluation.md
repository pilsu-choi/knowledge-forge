# Search Evaluation / Feedback Loop + Groundedness Gate

`search-architecture.md`가 "Search Evaluation / Feedback Loop — 설계만, 미구현"과
"Iterative / Agentic Retrieval — 이미 구현됨(암묵적 판단에 의존)"으로 남겨둔 두 항목을 채우는
네 조각 - (A)/(B)는 구현됨, (C)는 골든셋·지표 모듈과 `eval-ir` CLI까지 구현됐고, (D)는 여전히 설계만이다. (A)가 나머지 전부의 전제조건이다.

## 스코프: 독립적인 네 조각

| 조각 | 내용 | 골드셋 필요 여부 | 상태 |
|---|---|---|---|
| (A) 로깅 인프라 | query/answer/evidence를 Postgres에 기록 | 불필요 | **구현됨** |
| (B) Groundedness/Citation 자동 평가 | LLM-as-judge로 claim-evidence 매칭, 비동기 배치 | 불필요 | **구현됨** |
| (C) Recall/Precision/MRR/NDCG | relevance judgment 필요 | **필요** | **구현됨**(골든셋 v0·지표 모듈·`eval-ir`/`eval-gen` CLI). v1-draft/v2-draft는 사람 검수 대기 |
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

## (C) Recall / Precision / MRR / NDCG — 골든셋·지표·`eval-ir`/`eval-gen` CLI 구현됨, v1 검수가 다음

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
| `evaluation/ir_runner.py` | `run_ir_eval`: 골든셋 질의마다 `search/retrieve.py::retrieve` 호출 → `score_query` → `aggregate`, 결과를 `EvalRun`으로. retriever는 `retrieve_fn`으로 주입(기본값은 지연 import) |
| `evaluation/gen.py` | `eval-gen`의 생성 파이프라인: `stratified_sample`/`generate_for_chunk`/`reject_reason`/`build_golden_query`/`verify_queries`/`generate_golden` (DB·LLM 모두 주입) |
| `agent/providers/claude_agent_sdk.py` | 로컬 `claude` CLI 경유 텍스트 전용 ChatProvider(평가 전용, 도구 호출 거부) |
| `evaluation/ir_compare.py` | `compare_runs`: 두 run의 paired delta + bootstrap CI + win/loss/tie + 유형별 delta |
| `storage/eval_store.py` (`EvalRun`/`EvalRunQuery`) | `save_eval_run`/`save_eval_run_queries`/`get_eval_run`/`list_eval_runs`/`list_eval_run_queries`, `snapshot_index`, `content_hashes_for` |
| `cli.py::eval-ir` | 실행·`--compare`·`--list` 세 모드 |
| `tests/evaluation/test_ir_runner.py`, `test_ir_compare.py`, `tests/storage/test_eval_store_ir.py`, `tests/test_cli.py` | fake retriever로 러너 로직, 손계산 비교, DB 왕복(테스트 전용 `test-ir-` id), CLI 스모크 |

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

### `knowledge-forge eval-ir` — 실행 기록과 A/B 비교

```bash
# 실행: v0 전체, LLM 제약 추출 없이, 결과를 DB + JSON에 기록
knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --label baseline --json out/v0-baseline.json
# 일부만: 유형·질의 id 필터, 전략 override, 결과 수
knowledge-forge eval-ir --golden tests/eval/golden/v0.json --type set_query,aggregation --strategies lexical --limit 20
# 비교 / 목록
knowledge-forge eval-ir --compare <run_a> <run_b> --metric ndcg@10
knowledge-forge eval-ir --list --golden-version v0
```

옵션: `--project-id`(골든셋의 `project_id`가 있으면 그쪽 우선), `--limit 10`, `--k 1,3,5,10`,
`--strategies vector,lexical`(라우팅 무시 — 적은 전략**만** 돌아간다. 어휘 arm도
`keyword_arm`으로 적어야 도니 하이브리드를 재현하려면 `vector,keyword_arm`이다), `--no-llm`(provider 생성 안 함,
`use_llm_constraints=False`), `--provider/--model`(`eval`과 동일), `--label`, `--type`(multi-label
`types` 중 하나라도 맞으면 포함), `--query-id`(반복), `--concurrency 4`, `--no-store`(DB 미기록 —
`index_snapshot`/staleness 검사도 생략), `--json PATH`. `KF_DATABASE_URL`은 세 모드 모두 필수.
출력은 전체 지표 한 줄 + 유형별 표(n, hit@1, recall@5, mrr, ndcg@10, exhaustive_recall,
count_match, none_correct 중 존재하는 열) + 실패 질의 + stale 질의 + `run_id`.

러너(`evaluation/ir_runner.py::run_ir_eval`)가 하는 일, 순서대로:

1. `eval_store.snapshot_index(project_id)`로 **실측** `index_snapshot`(documents/chunks/
   knowledge_objects + `knowledge_object_embeddings`를 target_kind별로) — 골든셋 파일의 값을
   복사하지 않는다. 드리프트를 잡는 건 실측값이다.
2. 정답 문서들의 `sources.content_hash`를 가져와 `check_staleness` — stale 항목은 **경고만**
   (`summary.stale_queries`)하고 집계에서 빼지 않는다. 빼면 이전 run과 비교가 안 되고,
   표시하면 점수 하락을 드리프트 탓으로 돌릴 수 있다.
3. 질의마다 `retrieve(q.query, store=, provider=, project_id=, now=q.as_of, limit=, strategies_override=)`.
   `exhaustive`/`count` 질의는 **limit을 200으로 승격** — 전체 결과 대비 채점인데 10개로 자르면
   검색 품질과 무관하게 실패한다. 예외는 그 질의의 `error`에 담고 계속(전체 실패 아님).
4. `score_query(result.document_ids, expected, k_values, predicted_count=, scores=)` →
   `aggregate` → `eval_run.summary`. `config_hash`는 config JSON(정렬 직렬화)의 sha256 앞 12자,
   `git_commit`은 `git rev-parse HEAD`(실패 시 NULL).

**스키마** (`storage/schema.sql`):

| 테이블 | 열 |
|---|---|
| `eval_run` | `run_id` PK, `golden_version`, `golden_path`, `project_id`, `config` JSONB(provider/model/limit/strategies_override/use_llm_constraints/k_values), `config_hash`, `index_snapshot` JSONB, `git_commit`, `label`, `started_at`, `finished_at`, `summary` JSONB(`aggregate` 결과 + errors/stale_queries) |
| `eval_run_query` | PK(`run_id` FK CASCADE, `query_id`), `query_text`, `primary_type`, `types`, `ranked_document_ids`, `scores`, `predicted_count`, `metrics`, `strategies`, `steps`, `elapsed_ms`, `error` |

run 행은 시작 시 한 번(summary 비움), 종료 시 한 번 더 upsert — 중간에 죽어도
`finished_at IS NULL`인 흔적이 남는다. 질의별 랭킹과 step을 통째로 남기는 이유는 회귀가 났을 때
검색을 다시 돌리지 않고 어느 전략이 무엇을 반환했는지 볼 수 있어야 해서다.

**A/B 비교 규칙** (`evaluation/ir_compare.py::compare_runs`):

- 같은 `golden_version`이 아니면 `ValueError` — 질의 모집단이 다르면 paired delta가 무의미하다.
- `index_snapshot`이 다르면 비교는 하되 `snapshot_mismatch`/`warnings`로 표시 — 재인제스트로
  검색 가능한 것 자체가 바뀌었을 수 있다. 같은 `config_hash`끼리 비교하면 "run-to-run 노이즈"
  경고.
- 두 run이 모두 채점한 질의만 짝지어 B−A(`ir_metrics.paired_delta`), 헤드라인 지표(기본
  `ndcg@10`)에 `bootstrap_ci`(seed 고정, 결정론) + win/loss/tie + 유형별 delta, 나머지 공유
  지표는 `per_metric`에 평균 delta만. `error`가 난 질의는 짝에서 빠진다(`n`이 알려줌).

## `knowledge-forge eval-gen` — 골든셋 초안 자동 생성 (구현됨)

`evaluation/gen.py` + `cli.py::eval-gen`. **층화 샘플링 → LLM 질의 생성 → 자동 필터 →
검색 되돌리기 검증**까지 돌고 `origin: "synthetic"` 골든셋 **초안**을 낸다. 사람 검수는
이 커맨드 밖이다.

```bash
# 청크 12개를 층화 샘플링해 청크당 2개씩 생성, 최대 20개 채택
KF_DATABASE_URL=postgresql://... knowledge-forge eval-gen \
  --provider claude_agent_sdk --chunks 12 --per-chunk 2 --max-queries 20 \
  --out tests/eval/golden/v1-draft.json --stats-json out/gen-stats.json
```

옵션: `--chunks`(샘플 청크 수), `--per-chunk`, `--max-queries`, `--min-tokens 80`(청크 노이즈
하한), `--source-type slack,gmail`, `--project-id`, `--seed 0`, `--max-per-document 2`,
`--provider/--model`, `--concurrency 4`, `--no-verify`, `--verify-k 10`, `--force`,
`--stats-json`. `KF_DATABASE_URL` 필수.

### 왜 이렇게 만들었나

- **원천은 `chunks.text`(원문 청크)이지 KnowledgeObject가 아니다.** KO에서 뽑으면 추출 품질이
  골든셋 품질의 상한이 되고(추출이 놓친 사실은 질문이 될 수 없다), `--no-extract` 경로로
  인제스트한 코퍼스에서는 아예 돌지 않는다. 청크를 원천으로 두면 "KO 임베딩 vs 청크 임베딩"
  (`KF_EMBEDDING_SOURCE`) A/B도 같은 골든셋으로 잰다.
- **임베딩에 의존하지 않는다.** 샘플링은 SQL 필터 + 결정적 층화, 검증은 렉시컬/메타데이터
  경로만으로도 돈다 — `OPENAI_API_KEY` 없는 환경에서 생성이 멈추면 안 된다.
- **층화는 비례가 아니라 라운드로빈**(`source_type`별). 개발 DB는 google_drive 246 / slack 197 /
  gmail 59로 치우쳐 있어 비례 배분이면 소수 소스가 사실상 빠지고, 유형별 집계가 한 소스의
  성능이 된다. `--max-per-document`는 청크가 많은 긴 문서 하나가 샘플을 독점하는 걸 막는다.
- **생성 가능한 유형은 11종뿐**(`gen.py::SINGLE_CHUNK_TYPES`: factoid/specific_document_qa/
  navigational/whole_document_summary/exact_match/entity_centric/temporal/metadata_filter/
  parent_child/conversation_thread/long_range_context). 나머지 유형은 `golden.py` 로더가 정답
  모양을 강제하는데(aggregation→`count`, set_query→`exhaustive`, negative/null→`none=true`,
  relative_temporal/latest→`as_of`, ambiguous/multihop/비교→문서 2개 이상) 그 모양은 청크
  하나에서 나오지 않는다. 억지로 만들면 "정답이 한 문서뿐인 set_query" 같은 거짓 라벨이 된다.
- **자동 필터는 버리는 것과 표시하는 것을 나눈다.**
  - *버림*: 청크 문장을 그대로 베낀 질의(질의와 청크의 최장 공통 부분 문자열 ≥ 25자 —
    한국어는 조사 때문에 토큰 자카드보다 이쪽이 복사를 훨씬 잘 가른다), 12자 미만, 중복,
    생성 불가 유형, 스키마 위반.
  - *표시*: 생성된 질의를 `search/retrieve.py::retrieve`로 되돌려 검색해 정답 문서가 상위 k에
    없으면 `notes`에 `[review:hard]`. **버리지 않는 이유**: 검색이 못 찾는 질의를 빼면 골든셋이
    "항상 좋은 점수가 나오는" 자기충족 예언이 된다. 어려운 질의야말로 개선을 측정할 여지가
    있고, 진짜 모호한 것과 검색이 약한 것을 가르는 건 사람 몫이다. 검증은 provider 없이
    (규칙 기반 제약 추출만) 돌아 결정적이다.
- **출력은 항상 draft**: `--golden-version`과 `--out` 파일명 양쪽에 "draft"가 없으면 CLI가
  거부하고, 이미 있는 파일은 `--force` 없이 덮어쓰지 않는다. 사람 검수를 안 거친 결과가
  정식 버전 파일로 위장하면 과거 eval run과의 비교가 조용히 깨진다.
- 통과 질의가 0개면 빈 파일을 쓰지 않고 통계만 출력한 뒤 exit 1 — 빈 골든셋은 로더가 거부한다.

### LLM provider — `claude_agent_sdk` (신규, 평가 전용)

`agent/providers/claude_agent_sdk.py::ClaudeAgentSDKChatProvider`. 로컬 `claude` CLI를 통해
한 번 호출하는 **텍스트 전용** provider로, ANTHROPIC_API_KEY 없이(Claude Code 로그인만으로)
`eval`/`eval-ir`/`eval-gen`을 돌리기 위한 것이다. `config.py`의 `agent_provider` Literal에는
**일부러 넣지 않았다** — wiki/agent-runtime.md의 "claude_agent_sdk는 harness의 어떤 모드에도
들어오지 않는다"는 판단은 그대로다. `get_chat_provider(provider="claude_agent_sdk")`로
명시적으로 요청할 때만 만들어지고, `tools`가 비어 있지 않으면 `NotImplementedError`를 던진다.

### 첫 생성 결과 (2026-09-04, `tests/eval/golden/v1-draft.json`)

청크 12개(gmail 4 / google_drive 4 / slack 4) → 생성 20개 → `copied_from_chunk` 2개 버림 →
**채택 18개**, 그중 **12개가 `[review:hard]`**. 유형은 factoid 9 / conversation_thread 5 /
temporal 3 / exact_match 1.

flag 비율이 높은 건 대체로 생성기 문제가 아니라 **검증 환경의 한계**다: 임베딩이 0행이라
`vector`가 항상 비어 렉시컬/메타데이터로만 검증되고, slack/gmail 청크 수준의 질문은 문서
제목에 단서가 없어 pg_trgm으로 잡히지 않는다. 임베딩을 채운 뒤 다시 검증하면 상당수가
풀릴 것으로 본다. 눈으로 본 품질 소견:

- 잘 나온 것: 문서명/식별자가 질의에 박힌 것(`프로젝트 투입 관리_2026-031_...`, Google Sheets
  링크 `exact_match`), 기술 문서의 규칙을 묻는 factoid. 정답도 `notes`에 정확히 적힌다.
- 약한 것: **문맥 의존 질의**가 남는다("3차 인터뷰에 사용하기 위해 전달된 파일은?"처럼 여러
  문서에 걸릴 수 있는 질문). 프롬프트가 "가리키는 말 대신 고유명사를 넣어라"라고 지시해도
  청크가 짧은 slack 메시지면 넣을 고유명사 자체가 없다 — 채널 id(`#C0BSW8Y2AD9`)로 대신
  채우는데, 사람이 실제로 칠 질의는 아니다. 검수 때 이 부류를 걷어내거나
  `--source-type gdrive`로 소스를 좁히는 게 현실적이다.
- 개인 이름은 실제로 들어가지 않았다(프롬프트 규칙이 지켜짐).

### 두 번째 생성 결과 (2026-09-04, `tests/eval/golden/v2-draft.json`) — gdrive 한정 재생성

1차의 최대 약점(짧은 slack 청크 → 채널 id로 때운 질의)이 소스 편향에서 온다는 가설을 검증하려고
**`--source-type google_drive`로 좁혀 더 크게** 다시 뽑았다.

```bash
knowledge-forge eval-gen \
  --source-type google_drive --chunks 30 --per-chunk 2 --max-queries 40 \
  --max-per-document 13 --min-tokens 80 --seed 0 \
  --provider openai_compatible --model anthropic/claude-sonnet-5 \
  --golden-version v2-draft --out tests/eval/golden/v2-draft.json \
  --stats-json out/gen-stats-v2.json
```

통계(`out/gen-stats-v2.json`, `out/`는 gitignore라 여기에 남긴다):

```json
{"candidates": 32, "sampled_chunks": 30, "sampled_by_source_type": {"google_drive": 30},
 "llm_calls": 30, "llm_failures": 1, "raw_queries": 58,
 "rejected": {"copied_from_chunk": 8}, "accepted": 40, "verified": 40, "flagged_hard": 5}
```

유형 분포: factoid 21 / parent_child 8 / exact_match 5 / specific_document_qa 3 /
entity_centric 2 / temporal 1. 검증 rank 분포: rank1 19, rank2 13, rank3 2, rank4 1,
미발견(`[review:hard]`) 5.

#### provider를 `openai_compatible`(OpenRouter)로 바꿨다

1차의 `claude_agent_sdk`는 그 시점에 API 키가 없어 로컬 `claude` CLI를 경유한 것이었다.
지금은 `.env`에 `OPENROUTER_API_KEY`가 있으므로 `get_chat_provider(provider="openai_compatible")`
+ `anthropic/claude-sonnet-5`를 썼다. 서브프로세스로 CLI를 다시 부르지 않아 실패 모드가 단순하고,
JSON을 코드펜스 없이 그대로 뱉어 `parse_generation_response`의 복구 경로를 탈 일이 없다
(1차에서 CLI가 JSON 앞뒤에 설명을 붙이던 문제). 30회 호출 중 파싱 실패는 1회.

**함정**: `.env`의 `OPENROUTER_API_KEY`는 pydantic-settings가 `os.environ`에 넣어주지 않는다
(`Settings(env_prefix="KF_")`라 접두사 없는 키는 로드 대상이 아니고, `config.py` 주석대로
"OpenRouter provider가 직접 환경에서 읽는다"는 전제). 그런데 `OpenAICompatibleChatProvider`는
`os.environ.get("OPENROUTER_API_KEY")`로 읽으므로, `.env`에만 있으면 `api_key="unset"`으로
401이 난다. 실행 셸에서 명시적으로 export해야 한다. 이건 알려진 구멍이고 여기서 고치지 않았다.

#### 파라미터를 조정한 이유 — gdrive 코퍼스가 생각보다 얕다

`--max-per-document 13`은 임의 값이 아니라 **어쩔 수 없이** 올린 값이다. 실측:

| source_type | `min_tokens=80` 후보 청크 | 그 청크가 속한 문서 수 |
|---|---|---|
| google_drive | **32** | **7** |
| slack | 124 | 124 |
| gmail | 101 | 52 |

`sources_by_type`의 google_drive 246은 *source* 행 수이지 청크를 가진 문서 수가 아니다.
실제로 80토큰 이상 청크를 가진 gdrive 문서는 **7개뿐**이고, 32개 청크 중 **27개가 문서 2개**에
몰려 있다(15개 + 12개). 기본값 `--max-per-document 2`로는 gdrive 전체에서 14개밖에 못 뽑아
`--chunks 30`이 성립하지 않는다. 30을 채우려면 상한이 13이어야 한다(13+12+1×5=30).
`--min-tokens`는 80을 유지했다 — 40으로 내리면 후보가 50개로 늘지만 문서 수는 그대로 7이고,
"짧은 청크 → 앵커 없는 질의"가 1차의 실패 원인이었으므로 하한을 낮출 이유가 없다.

#### 가설 검증: gdrive 한정은 **질의 문장 품질에서는 확실히 나았고, 골든셋 설계로는 나쁘다**

*나아진 것 — 채널 id 문제는 완전히 사라졌다.* 40개 중 소스 내부 식별자로 때운 질의는 0개다.
대부분이 사람이 실제로 검색창에 칠 법한 문장으로 나왔다. gdrive 문서가 제목·머리말·표를 갖춘
기술 문서라 프롬프트 규칙 3("가리키는 말 대신 고유명사를 넣어라")을 지킬 재료가 실제로 있었다.

- `ProjectIntel Codex 작업 시 기준 브랜치는 무엇인가요?` (factoid, 정답 `deploy-main`, rank=1)
- `dashboard 502 에러 발생 시 처음 확인해야 할 3가지는?` (factoid, rank=1)
- `대시보드 Playwright e2e 테스트에서 로컬 포트가 겹칠 때 사용하는 환경변수는 무엇인가?` (exact_match)

*그대로 남은 것 — 앵커 없는 질의.* 청크가 규칙 목록이면 고유명사가 아예 안 나오는 문단이 있고,
그럴 때 1차와 같은 실패가 형태만 바꿔 재발한다. 어느 문서를 묻는지 사람도 특정할 수 없다:

- `기존 문서를 대체할 때 삭제하는 대신 어떤 표시를 우선해야 하는가?`
- `MR/PR 제목과 본문은 어떤 언어로 작성해야 하나요?`
- `바이브 코딩은 AI에게 크게 맡기는 방식인가요?` (청크 첫 문장을 그대로 뒤집은 예/아니오 질문)

`copied_from_chunk`는 8건을 잡았고 남은 40개에 통문장 복사는 없다. 다만 위 3번째처럼
**문장을 부정문/의문문으로 살짝 비틀면 최장 공통 부분 문자열 25자 기준을 빠져나간다** —
필터가 잡는 건 복사지 "청크 문장 1개를 그대로 되묻는 것"이 아니다.

*나쁜 것 — 정답 문서가 5개뿐이다.* 채택 40개의 정답 문서 분포는 20 / 14 / 2 / 2 / 2로,
**34개가 문서 2개에 몰린다**. 문서 263개 코퍼스에서 40개 질의가 사실상 5-way 문제가 되므로,
이 상태로 nDCG/MRR을 재면 "그 두 문서를 얼마나 잘 찾나"를 재는 것이지 검색 성능이 아니다.
1차가 18개 질의로 소스 3종을 덮은 것보다 **평가셋으로서는 후퇴**다.

*`[review:hard]` 5/40(12.5%)은 1차 12/18(67%)과 비교하면 안 된다.* 1차 때는 임베딩이 0행이라
`vector` arm이 항상 비어 있었고, 지금은 청크 936개가 임베딩돼 있다. 조건이 다르다.
게다가 정답 문서가 5개뿐이면 되돌려 검색이 쉬워지는 게 당연하다(rank1+rank2가 32/40).
낮은 flag 비율을 생성 품질 개선의 증거로 읽으면 안 된다.

**결론**: 소스를 gdrive로 좁히는 건 *질의 문장*을 고치는 데는 효과가 있었지만, 이 개발 DB의
gdrive는 문서 7개짜리 얕은 코퍼스라 **골든셋 하나를 통째로 gdrive로 만들면 안 된다**.
현실적인 다음 수는 (a) v2-draft에서 앵커 없는 질의를 검수로 걷어내 gdrive 파트로만 쓰고,
(b) slack/gmail은 청크 길이 하한을 올리거나(`--min-tokens`를 150+로) 스레드 단위로 합친 뒤
따로 생성해 합치는 것이다. gdrive 문서를 더 인제스트하기 전에는 gdrive 단독 확대는 의미가 없다.

### 다음 단계

1. **v1-draft 사람 검수 → v1 발행.** `[review:hard]` 12개부터 본다. 검수를 마치면 파일명을
   `v1.json`으로 굳히고(그때부터 불변) 첫 절대값 기준(Tier A)으로 쓴다.
2. **청크 하나로 못 만드는 유형**(aggregation/set_query/negative/null/multihop/비교/
   relative_temporal/latest/ambiguous)은 여전히 손 작성이거나 별도 생성기가 필요하다 —
   v0의 그 항목들을 v1로 옮겨 오는 게 가장 싸다.
3. **`eval-gen --from-traffic`** — `search_feedback.rating='up' ∧ groundedness ≥ 0.9`인
   `search_log`의 evidence를 `origin: "feedback"`으로 편입(Tier C). 노이즈가 크므로 큐레이션 셋을
   대체하지 않는다.
### v0 첫 실측 baseline (2026-09-04)

`knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --label baseline`
→ **`run_id=fc96003a531547a0b04267a2ca7b2844`**. 이후 전략을 바꾸면 이 run과 `--compare`한다.

개발 DB 기준, **`--no-llm`(규칙 기반 제약 추출만)이고 임베딩 0행이라 `vector`는 항상 비어
키워드 fallback으로 돈다** — 아래 숫자는 렉시컬/메타데이터/문서 조회만의 성능이다.

```
overall: n=76 hit@1=0.657 recall@5=0.697 mrr=0.706 ndcg@10=0.709
         exhaustive_recall=0.778 count_match=0.500 none_correct=1.000
```

| 잘 되는 유형 | ndcg@10 | 안 되는 유형 | ndcg@10 |
|---|---|---|---|
| long_range_context / parent_child | 1.000 | **multihop** | 0.000 |
| exact_match | 0.952 | conversation_thread | 0.250 |
| version | 0.947 | sequence | 0.343 |
| ambiguous | 0.941 | factoid | 0.497 |
| navigational | 0.830 | latest | 0.544 |

읽는 법: 제목·식별자가 질의에 그대로 있는 유형(navigational/exact_match/version)은 pg_trgm만으로
거의 다 잡힌다. **multihop 0.000은 `--no-llm`이면 planner가 아예 안 도는 게 원인**이고(검색 품질
문제가 아니다), factoid 0.497은 패러프레이즈를 벡터 없이 푸는 한계다. `count_match=0.500`은
"본문에 언급만 된 문서"를 정답 집합에 셀지에 대한 골든셋 v0의 판단이 흔들린다는 신호 — v1
검수 때 정리 대상이다.

다음 baseline은 (1) `OPENAI_API_KEY` + `KF_EMBEDDING_SOURCE=chunk`로 임베딩을 채우고,
(2) `--no-llm` 없이(=planner/LLM 제약 추출 켜고) 다시 돌려 세 조건을 `--compare`로 비교한다.

### term 단위 키워드 fallback — 두 번째 실측 (2026-09-04)

baseline의 conversation_thread(0.250)/sequence(0.343)를 `steps`로 파고든 결과, 원인은
`document_links` 미사용이 아니라 **키워드 fallback이 질의 문자열을 통째로 pg_trgm에 넘겨
히트가 0**이었던 것이다(자세한 설명은 `search-architecture.md`의 "키워드 fallback"). 질의를
term으로 쪼개 term마다 `lexical_search`를 부르고 소스를 나눠 RRF에 넣도록 바꿨다.

먼저 아무 안전장치 없이 돌리자 재현율은 크게 올랐지만 **정밀도 계열이 무너졌다**
(`run_id=0f4d097f3c984c98bf37a1a087006f4f`): ndcg@10 0.709 → 0.814인데 `none_correct`
1.000 → 0.000, `count_match` 0.500 → 0.000. term 하나만 스친 문서까지 후보가 되어 "관련 문서
없음"이 정답인 질의가 전부 무언가를 돌려준 것이다.

그래서 (a) **coverage 임계값**(질의 term을 `ceil(n × ratio)`개 이상 담은 문서만)과
(b) **1단계(전체 질의)가 빈손일 때만 실행**하는 게이트를 붙이고 `KF_TERM_COVERAGE_RATIO`로
스윕했다. 전부 `--no-llm`, 임베딩 0행, v0 골든셋 76개:

| ratio (min=2) | 게이트 | hit@1 | recall@5 | mrr | ndcg@10 | exh_recall | count | none |
|---|---|---|---|---|---|---|---|---|
| — (baseline) | — | 0.657 | 0.697 | 0.706 | 0.709 | 0.778 | 0.500 | 1.000 |
| 0 (필터 없음) | 없음 | 0.771 | 0.810 | 0.817 | 0.814 | 0.921 | **0.000** | **0.000** |
| 0.34 | 없음 | 0.757 | 0.817 | 0.816 | 0.809 | 0.853 | 0.500 | 0.333 |
| 0.5 | 없음 | 0.771 | 0.817 | 0.828 | 0.823 | 0.842 | 0.500 | 0.500 |
| 0.6 | 없음 | 0.786 | 0.826 | 0.839 | 0.828 | 0.829 | 0.500 | 0.833 |
| 0.67 | 없음 | 0.771 | 0.795 | 0.821 | 0.808 | 0.806 | 0.500 | 1.000 |
| 0.75 | 없음 | 0.729 | 0.776 | 0.782 | 0.776 | 0.794 | 0.500 | 1.000 |
| 0.34 | 있음 | 0.771 | 0.836 | 0.834 | 0.824 | 0.837 | 0.500 | 0.333 |
| 0.5 | 있음 | 0.800 | 0.836 | 0.852 | 0.838 | 0.826 | 0.500 | 0.500 |
| **0.67** | **있음** | **0.800** | **0.823** | **0.847** | **0.834** | **0.806** | **0.500** | **1.000** |
| 1.0 | 있음 | 0.686 | 0.726 | 0.735 | 0.738 | 0.778 | 0.500 | 1.000 |

**2/3 + 게이트를 기본값으로 골랐다** — baseline 대비 **어느 지표도 나빠지지 않는 유일한 지점**이다.
0.5~0.6이 ndcg는 조금 더 높지만 `none_correct`가 1.000에서 내려간다. "관련 문서 없음"을 못 말하는
검색은 재현율 몇 점과 바꿀 게 아니라고 보고 보수적인 쪽을 택했다.

채택 run **`run_id=350e721aec6247fa81ac6c8057159486`** (label `gated-r0.67-m2`), baseline과의
paired 비교:

```
knowledge-forge eval-ir --compare fc96003a531547a0b04267a2ca7b2844 350e721aec6247fa81ac6c8057159486 --metric ndcg@10
mean A=0.7092 B=0.8342 delta(B-A)=+0.1250 95% CI [+0.0564, +0.2039] 유의
win/loss/tie = 10/0/60
```

유형별로는 factoid 0.497 → 0.969, conversation_thread 0.250 → 0.750, entity_centric +0.273,
specific_document_qa +0.200, multihop +0.271(planner가 아니라 근거 문서가 fallback으로 잡힌 것),
sequence +0.117. **loss가 0건**이다.

남은 낮은 유형과 그 성격:

| 유형 | ndcg@10 | 성격 |
|---|---|---|
| sequence | 0.460 | 시간 순서를 랭킹에 안 쓴다. `document_links.thread_sequence`/`same_channel_sequence`는 백필돼 있지만 `retrieve()`가 아직 읽지 않는다 |
| latest | 0.544 | `latest` 후처리가 도는데도 낮다 — 정답 쪽 문제인지 실행기 문제인지 미확인 |
| metadata_filter | 0.609 | 작성자/참여자 필터가 Slack user id에 막힌다 |
| navigational | 0.671 | LLM 없이는 자연어 제목 참조를 `document_reference`로 못 뽑는다 |
| temporal | 0.562 / relative_temporal 0.667 | 시간 파싱 + 개발 DB의 시각 분포 |

**A/B 도구의 한계 하나**: `--compare`가 "both runs share config_hash — the delta measures
run-to-run noise"라고 경고했지만 실제로는 코드가 바뀐 A/B였다. `config_hash`가 설정값만 보고
검색 코드 버전을 안 본다 — 코드/파이프라인 버전을 해시에 넣어야 이 경고가 쓸모 있어진다.

### `latest` 후처리 수정 — 세 번째 실측 (2026-09-04)

term fallback 이후 남은 낮은 유형을 `steps`로 파고든 결과, `latest` 0.544의 원인은 골든셋이
아니라 실행기였다. `_Run.latest`가 **후보 전체를 시각 내림차순으로 재정렬**해서 병합 랭킹을
통째로 버리고 있었다(`search-architecture.md` "`latest`가 관련도를 버리지 않게 하는 이유").
시각 정렬 대상을 관련도 상위 pool로 좁혔다(`KF_LATEST_RELEVANCE_POOL`).

pool 스윕(전부 `--no-llm`, ratio 2/3, v0 76개):

| pool | hit@1 | ndcg@10 | latest | version |
|---|---|---|---|---|
| 40 / 20 / 10 | 0.814 | 0.840 | 0.544 | 0.947 |
| 7 | 0.815 | 0.840 | 0.544 | 0.947 |
| **5** | **0.829** | **0.845** | **0.667** | **1.000** |

채택 run **`run_id=f878d05c733642039298eb07c668fe53`** (label `latest-pool5`). 원 baseline과의
paired 비교: mean 0.7092 → 0.8455, delta **+0.1363**, 95% CI [+0.0627, +0.2182] 유의,
win/loss/tie **12/1/57**.

직전 채택본(term fallback만) 대비로는 +0.0113, CI [-0.0195, +0.0497]로 **유의하지 않다** —
`latest`/`version` 유형(10개)에만 걸리는 수정이라 76개 평균으로는 묻힌다. 유형별로는
latest 0.544 → 0.667, version 0.947 → 1.000, navigational 0.671 → 0.830, sequence 0.460 → 0.710.

유일한 loss는 `v0-set_query-002`(0.936 → 0.359)인데, 이건 pool이 아니라 **coverage ratio를
0.67에서 2/3으로 바꾼 부작용**이다. 임계값이 `ceil(term수 × ratio)`라 term이 3·6·9개일 때
두 값의 임계값이 갈린다(2/3 → 2·4·6, 0.67 → 3·5·7). 이 질의는 임계값이 3에서 2로 내려가면서
`metadata_filter`가 `filter_only`(197건, 정답 10건 포함)에서 `intersected`(2건)로 바뀌었다.
pool=5 고정 상태에서 두 ratio를 따로 재 보면 2/3이 0.67보다 전체적으로 낫다
(ndcg@10 0.845 vs 0.839, hit@1 0.829 vs 0.814; 대신 set_query는 0.749 vs 0.783). 전체를 택했다.

**교훈**: ratio를 소수점 두 자리로 어림해 적으면 안 된다. 계단이 지는 지점이라 반올림처럼
보이는 차이가 랭킹을 바꾼다.

### 제약 추출 버그 4건 수정 — 네 번째 실측 (2026-09-04)

`steps` 진단에서 나온 제약 추출 버그 4건(시간 표현의 한글 수사, count 분류사 "통",
"발신자"를 사람 이름으로 오인, 이메일 도메인 미추출)을 고쳤다.
채택 run **`run_id=8c12c099e3c749178881d7b51e6422de`**.

| | 직전(`latest-pool5`) | 지금 |
|---|---|---|
| hit@1 | 0.829 | **0.857** |
| recall@5 | 0.833 | **0.848** |
| mrr | 0.868 | **0.890** |
| ndcg@10 | 0.845 | **0.865** |
| exhaustive_recall | 0.812 | **0.846** |
| count_match | 0.500 | 0.333 (아래) |
| none_correct | 1.000 | 1.000 |

유형별: relative_temporal 0.667 → **1.000**, metadata_filter 0.598 → **0.767**,
set_query 0.749 → 0.828. paired: +0.0193, CI [+0.0000, +0.0571]로 유의하지는 않지만
**win/loss/tie 2/0/68**로 loss가 없다(4건짜리 국소 수정이라 76개 평균에 묻힌다).

**`count_match` 0.500 → 0.333은 회귀가 아니라 노출이다.** `v0-aggregation-002`("메일 몇 **통**")
가 그동안 `COUNT_MARKERS`에 "통"이 없어 `count` 전략 자체가 선택되지 않았고,
`predicted_count=None`이라 **채점에서 빠져 있었다**(분모 2). 이제 전략이 돌면서 분모가 3이 됐고
`predicted_count=25`(정답과 불일치)로 틀렸다. 즉 이전 0.500은 실패를 세지 않아서 나온 값이다.
원인은 아래 "남은 것" 6번(`count`가 `len(document_ids)`)이고, 이 질의가 그 증거다.

### 임베딩 적재 + 하이브리드 arm + 관련도 하한 — 다섯 번째 실측 (2026-09-04)

이 회차가 지금까지와 성격이 다르다. 1~4회차는 전부 **`vector_search`가 0히트인 상태**에서
잰 값이다(`knowledge_object_embeddings` 0행, 이 머신에 임베딩 키 없음). OpenRouter 임베딩
provider를 붙이고(`wiki/embeddings.md`) 청크 936개를 `openai/text-embedding-3-small`로
채운 뒤 처음으로 벡터 경로가 실제로 도는 상태에서 다시 쟀다.

| | 4회차 baseline (임베딩 0행) | 임베딩만 채움 | +어휘 arm | **+하한 0.45 (채택)** |
|---|---|---|---|---|
| run_id | `8c12c099e3c7…` | `9ad2bba29b4b…` | `d80b34428b68…` | **`bade74dc839b…`** |
| hit@1 | 0.857 | 0.671 | 0.786 | **0.829** |
| recall@5 | 0.848 | 0.844 | 0.886 | **0.897** |
| mrr | 0.890 | 0.784 | 0.859 | **0.884** |
| ndcg@10 | 0.865 | 0.804 | 0.870 | **0.886** |
| exhaustive_recall | 0.846 | 0.976 | 0.989 | **0.908** |
| count_match | 0.333 | 0.000 | 0.000 | **0.333** |
| none_correct | 1.000 | 0.000 | 0.000 | **1.000** |

**임베딩을 채우자 지표가 먼저 떨어졌다(0.865 → 0.804).** 짐작하지 말고 `steps`를 열어
원인을 찾았고, 두 가지였다.

**(1) 어휘 경로 전체가 "벡터가 실패했을 때만" 도는 fallback이었다.** `v0-navigational-004`
("Dev Handover README")의 baseline `steps`에는 vector가 자격증명 에러로 0히트가 되고
`fallback_from: "vector"`가 붙은 `document_search` 5건 + `lexical_search` 4건이 이어진다.
임베딩을 채우자 ANN이 관련도와 무관하게 늘 `limit`개를 돌려주면서 `if not hits` 조건이
성립하지 않아 **그 두 조회가 아예 실행되지 않았고**, vector 단독 30히트로 랭킹됐다.

즉 **1~4회차의 0.709 → 0.865는 "임베딩 없는 degrade 경로" 최적화가 아니라 degrade 경로
그 자체가 만든 점수다.** `suggest_strategies`가 `document_lookup`/`lexical`을 고르는 조건은
`identifiers`/`quoted_phrases`/`document_reference`가 잡혔을 때뿐이라 평범한 자연어 질의는
그 그물에 안 걸리는데, fallback은 semantic_query를 통째로 거는 훨씬 넓은 그물이었다.

→ fallback을 상시 하이브리드 arm으로 승격(`retrieve.py::_Run.keyword_arm`, 당시 이름은
`_keyword_arm`). 이미 전략이 돌았어도
끄지 않고 **소스 이름을 나눠**(`document_search#semantic`/`lexical_search#semantic`) RRF의
별도 투표자로 넣는다 — `_term_fallback`의 `lexical_search#<term>`과 같은 수법이고, 같은
소스에 append하면 서로 다른 질의의 순위가 한 리스트에 섞여 `1/(k+rank)`가 의미를 잃는다.
`_term_fallback`의 게이트(전체 질의 조회가 빈손일 때만)는 그대로 뒀다 — 항상 돌리면
`v0-navigational-002`가 깨진다는 3회차 실측이 유효하다. → 0.804 → **0.870**.

**(2) ANN에 관련도 하한이 없다.** 코퍼스에 전혀 없는 주제를 물어도 가장 가까운 `limit`개를
무조건 돌려주므로, "관련 문서 없음"이 정답인 negative/null 6개가 전부 틀리고
(`none_correct` 1.000 → 0.000) `count`가 부풀었다. 실측 점수 분포가 깨끗하게 갈린다:

| | top-1 코사인 유사도 |
|---|---|
| 정답 없음이 정답인 질의 6개 | 0.350 ~ 0.401 |
| 정답이 있는 질의 | 0.560 ~ 0.624 (예외: "주간 할 일 다이제스트" 0.359) |

→ `KF_VECTOR_MIN_SCORE`(기본 **0.45**). 스윕(0.0/0.35/0.40/0.42~0.48/0.50) 결과
**0.42~0.47이 ndcg@10 0.876~0.888의 고원**이고 그 구간 내내 `none_correct` 1.000 /
`count_match` 0.333이 유지된다. 0.40에서 `none_correct`가 0.833으로 무너진다.
**최고점은 0.42(0.888)지만 기본값은 고원 한가운데인 0.45로 잡았다** — v0는 사람 검수를
거치지 않은 스모크셋이라(아래 "v0의 성격") argmax를 박으면 과적합이고, 절벽과의 안전거리가
ndcg +0.002보다 낫다.

**정직하게 볼 것: baseline 대비 개선은 통계적으로 유의하지 않다.**

```
mean A=0.8648 B=0.8863 delta(B-A)=+0.0215 95% CI [-0.0274, +0.0680] 유의하지 않음
win/loss/tie = 9/6/55   (n=70, paired)
```

유형별로 크게 오른 것: conversation_thread +0.250, sequence +0.125, multihop +0.125,
temporal +0.105, cross_document_comparison +0.097. 내린 것: **navigational −0.096
(2승 4패)** — `v0-navigational-002`("codex README") 1.000 → 0.500이 대표. 제목이 정확히
맞은 질의에 벡터 후보가 섞이는 형태로, 3회차의 term fallback 회귀와 같은 종류다.

`exhaustive_recall`은 하한을 걸면서 0.989 → 0.908로 내려간다(baseline 0.846보다는 위).
재현율과 "없음을 없다고 말하는 능력"의 trade-off를 하한 하나로 조절하고 있는 셈이라,
`exhaustive`/`negative`에만 다른 하한을 쓰는 게 맞을 수 있다 — 미검증.

```bash
# 재현
export KF_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge
uv run knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --label <이름>
uv run knowledge-forge eval-ir --compare 8c12c099e3c749178881d7b51e6422de <new_run_id> --metric ndcg@10
# 하한 스윕
for v in 0.0 0.40 0.42 0.45 0.50; do KF_VECTOR_MIN_SCORE=$v uv run knowledge-forge eval-ir \
  --golden tests/eval/golden/v0.json --no-llm --label "vfloor-$v"; done
```

### 어휘 arm에도 anchor 판정 — 여섯 번째 실측 (2026-09-04)

다섯 번째 실측에서 유일하게 내려간 유형이 `navigational`(−0.096, 2승 4패)이었다. `steps`를
열어 보니 회귀한 6개 질의가 전부 **baseline에서 1위였던 정답이 2~3위로 밀린** 형태였고
(ndcg 1.000 → 0.631/0.500), 그 6개 모두 `anchor`가 미형성이었다.

원인은 이전부터 알려져 있던 구멍이다: **anchor 판정이 `document_lookup`에만 있고
`_keyword_arm`(옛 fallback)이 물어온 제목에는 안 걸렸다.** 그래서 제목 1.0 완전 일치가
anchor로 승격되지 못하고 RRF 한 표로 전락했다. arm이 fallback이던 시절에는 벡터가 죽어
있어서 경쟁자가 없었기 때문에 티가 안 났고, arm을 상시화하자 벡터 후보와 경쟁하면서 드러났다.

→ 판정 로직을 `_Run._claim_anchor()`로 뽑아 `document_lookup`과 `_keyword_arm`이 공유한다.
arm은 **anchor가 아직 없을 때만** 판정한다 — 이미 잡혀 있다면 그건 `document_reference`/
`identifiers` 같은 명시적 신호에서 나온 더 강한 anchor라 semantic_query가 덮어쓰면 안 된다.

| | 임베딩 0행 baseline | 하한 0.45 | **+arm anchor (채택)** |
|---|---|---|---|
| run_id | `8c12c099e3c7…` | `bade74dc839b…` | **`a8d8e118cc23…`** |
| hit@1 | 0.857 | 0.829 | **0.843** |
| recall@5 | 0.848 | 0.897 | **0.897** |
| mrr | 0.890 | 0.884 | **0.893** |
| ndcg@10 | 0.865 | 0.886 | **0.893** |
| exhaustive_recall | 0.846 | 0.908 | **0.908** |
| count_match | 0.333 | 0.333 | **0.333** |
| none_correct | 1.000 | 1.000 | **1.000** |

paired: delta **+0.0287, 95% CI [-0.0170, +0.0749]** — 여전히 유의하지 않다.
win/loss/tie 9/5/56. `navigational`은 −0.096 → **−0.055**(2승 3패)로 줄었다.

**남은 회귀 3건은 일부러 안 쫓았다** — v0가 사람 검수 안 된 76개 스모크셋이라 개별 질의를
맞추면 과적합이다. 다만 원인은 서로 다르고, 그중 둘은 진짜 능력 격차다:

| 질의 | 표면 원인 | 판단 |
|---|---|---|
| `v0-ambiguous-002` "README" | 제목 1.0 일치가 2건 → `ambiguous`로 anchor 포기 | **정상 동작.** 실제로 모호한 질의다 |
| `v0-navigational-004` "Dev Handover README 파일" | 실제 제목이 `README_ProjectIntel_Dev_Handover.md` — 토큰이 다 있지만 **순서가 다르고 `_` 구분자**라 substring 1.0이 안 나온다 | **능력 격차.** 제목 매칭이 순서 의존 substring이다 |
| `v0-navigational-003` "인수인계 pptx" | 실제 제목이 `projectintel-development-handover.pptx` — **한글 질의에 영문 제목**이라 "pptx"만 겹친다 | **능력 격차.** 교차 언어 제목 매칭 |

즉 `navigational` 잔여 회귀의 진짜 처방은 anchor 임계값을 낮추는 게 아니라 **제목 매칭을
토큰 집합 기반(순서 무관, 구분자 정규화)으로 바꾸는 것**이다. 임계값을 낮추면
`v0-ambiguous-002`처럼 진짜 모호한 질의에 엉뚱한 anchor가 박힌다.

### 어휘 arm에 이름 주기 — 일곱 번째 실측 (2026-09-04)

**지금까지의 "순수 벡터 baseline" A/B는 전부 무효였다.** `retrieve()`의 docstring은
`strategies_override=["vector"]`가 순수 벡터라고 적어 뒀지만, 다섯 번째 실측에서 arm을
상시화하면서 `_Run.vector()` **끝에 무조건 이어 부르는 부작용**으로 넣었기 때문에 그
override는 arm까지 돌리고 있었다. 실제 `steps`의 소스가 그 증거다 —
`["document_search#semantic", "lexical_search", "lexical_search#semantic", "vector_search"]`.

→ arm을 이름 있는 전략(`STRATEGY_KEYWORD_ARM = "keyword_arm"`)으로 꺼내고, 호출을
`retrieve()`의 전략 디스패치로 옮겼다. `suggest_strategies`는 `vector`를 넣을 때 항상 arm을
같이 넣으므로 **기본 라우팅은 한 자리도 안 움직인다**(아래 표 1열 = 여섯 번째 실측 채택값).
실행 순서(vector의 ANN → arm의 제목/본문 → 둘 다 빈손일 때만 `_term_fallback`)도 그대로 뒀다 —
`_claim_anchor`가 "먼저 잡힌 anchor를 덮어쓰지 않는다"에 기대고 있어 순서가 곧 지표다.

이제야 어휘 arm의 값을 잴 수 있다(76질의, `--no-llm`):

| | 기본 라우팅 | `vector,keyword_arm` | `keyword_arm` | `vector` |
|---|---|---|---|---|
| run_id | `f30be053f611…` | `084711f4169b…` | `3dd747a86b0f…` | `c8c6128714fb…` |
| hit@1 | **0.843** | 0.671 | 0.600 | 0.414 |
| recall@5 | **0.897** | 0.772 | 0.681 | 0.508 |
| mrr | **0.893** | 0.733 | 0.676 | 0.498 |
| ndcg@10 | **0.893** | 0.736 | 0.663 | 0.477 |
| exhaustive_recall | 0.908 | 0.708 | 0.599 | 0.384 |
| count_match | 0.333 | — | — | — |
| none_correct | 1.000 | 1.000 | 1.000 | 1.000 |

읽는 법 세 가지:

1. **"vector 단독 0.736"으로 기록돼 있던 값은 vector+arm이다.** 진짜 벡터 단독은 **0.477**로,
   arm 단독(0.663)보다도 낮다. 이 코퍼스에서 검색을 지탱하는 것은 어휘 쪽이고 벡터는 보강이다 —
   다섯 번째 실측의 "0.709 → 0.865는 degrade 경로가 만든 점수"라는 결론이 훨씬 강한 형태로 재확인된다.
2. **arm과 vector는 서로를 실제로 보강한다**: 0.663 + 0.477 → 0.736. RRF 융합이 두 팔 중
   나은 쪽보다 위다.
3. **후처리 전략의 몫이 크다**: 0.736(vector+arm) → 0.893(기본 라우팅)의 차이는
   `document_lookup`/`lexical`/`metadata_filter`/`latest`/`count` 등 라우터가 추가로 고르는
   전략들이다. `--strategies`는 후처리까지 포함해 목록을 통째로 대체하므로, 위 3개 열에는
   `count`/`latest`/`whole_document`가 없다(`count_match`가 `—`인 이유).

`exact_match`(0.186 → 0.675)와 `set_query`(0.364 → 0.796)가 arm으로 가장 크게 오른다 —
식별자·인용구가 임베딩에서 의미를 잃는다는 애초의 가설과 방향이 같다.

```bash
# 재현
uv run knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --label purity-default
uv run knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --strategies vector --label purity-vector-only
uv run knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --strategies vector,keyword_arm --label purity-vector-plus-arm
uv run knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --strategies keyword_arm --label purity-arm-only
```
### 어휘 source_type 힌트를 하드 필터에서 강등 — 여덟 번째 실측 (2026-09-04)

**증상.** `v1-draft`의 두 질의가 **완전히 같은 7건을 같은 순서로** 돌려줬다.

| id | 질의 | 정답 source_type |
|---|---|---|
| `v1-draft-factoid-003` | 애자일소다(프로젝트_인텔) Google Drive 폴더 공유 시 사용자에게 부여할 수 있는 역할은? | **gmail** |
| `v1-draft-temporal-002` | AgentX 2팀 Google Drive 폴더 액세스 요청이 언제 들어왔나? | **gmail** |

정답 2건은 `drive-shares-dm-noreply@google.com`이 보낸 Drive 공유요청 **알림 메일**이다.
질의의 "Google **Drive**"가 `SOURCE_TYPE_MARKERS`에 걸려 `source_types=['google_drive']`
하드 필터가 되고, 관련도 랭킹과 교집합이 0이 되자 `merge()`가 관련도 랭킹을 통째로 버리고
필터 집합을 최신순으로 냈다(`no_overlap_fallback_to_filter`). `document_search`가 정답을
score 1.0으로 1위에 잡아둔 채로 버려진 것이다. 질의에서 "Google Drive" 두 단어만 빼면 둘 다
즉시 1위가 된다.

**어휘 → source_type 매핑은 방향이 반대다.** 본문에 "Google Drive"/"드라이브"를 언급하는
문서의 실제 source_type(개발 코퍼스 실측): **gmail 11 / slack 4 / google_drive 3.** Drive에
저장된 원본 파일은 본문에 "Google Drive"라고 쓸 이유가 없고, 알림 메일은 항상 쓴다.

**측정하면서 두 번 방향을 틀었다.** 처음 두 시도가 왜 틀렸는지가 이 절의 본론이다.

1. **`no_overlap_fallback_to_filter`를 통째로 삭제** (필터를 항상 무시): 두 질의는 고쳐졌지만
   v0에서 `metadata_filter` ndcg 0.817→0.543, `relative_temporal` 1.000→0.578으로 무너졌다
   (`v0-metadata_filter-001` "nonghyup.com 도메인 발신자가 보낸 메일", `v0-relative_temporal-001`
   "최근 3일 안에 농협 측에서 온 메일"이 각각 1.000→0.000). 이유는 명확하다 — 이런 질의는
   제약을 걷어내고 나면 semantic_query가 "보낸" / "안에 농협 측 온"으로 쪼그라들어 **관련도 arm
   쪽이 쓰레기**다. 필터가 유일하게 맞는 신호인데 그걸 버렸다.
   → **필터의 신뢰도는 하나가 아니다.** 작성자/참여자/시간/제목 종류 같은 구조적 술어는
   사용자가 명시한 것이라 틀릴 수 없고, `source_type`은 정규식이 표면 어휘로 찍은 추측이다.
2. **교집합 0일 때만, 어휘 힌트뿐인 필터를 무시**: v0는 baseline과 완전히 동일(모든 지표 일치),
   두 질의도 1위 회복. 안전하지만 **교집합이 우연히 조금 생기는 경우를 못 막는다** — 실제로
   semantic_query를 함께 고치자(아래) 벡터 arm이 드라이브 파일 4건을 같이 물어와
   `intersected:52->4`가 되면서 1위 정답이 다시 잘려나갔다.
   → **교집합을 보기 전에 끊어야 한다.** 채택안: 어휘 힌트뿐인 필터
   (`_is_source_hint_only_filter`: WHERE가 `source_type IN (...)` 하나뿐)는 후보가 있는 한
   하드 필터로 쓰지 않는다(`applied="source_hint_not_applied"`). 구조적 술어가 하나라도 있으면
   기존 `intersected` / `no_overlap_fallback_to_filter` 동작 그대로.

**부수 수정: semantic_query 반쪽 스트리핑.** `\bg?drive\b`가 "Google Drive"에서 "Drive"만
지워 semantic_query가 `"애자일소다 Google 폴더 공유 시 ..."`가 됐다. 통째로 지우는 쪽과 통째로
남기는 쪽을 둘 다 재봤다(다른 조건 동일):

| | 반쪽(옛 동작) | 통째로 지움 | **통째로 남김 (채택)** |
|---|---|---|---|
| hit@1 | 0.843 | 0.814 | **0.857** |
| ndcg@10 | 0.893 | 0.890 | **0.897** |

"Google Drive"는 마커이면서 **동시에 코퍼스 본문의 내용어**라 남기는 쪽이 이겼다(공유요청 알림
메일 본문에 그대로 쓰여 있다). "드라이브"/"메일" 같은 맨 종류어는 정보량이 없어 그대로 지운다 —
예외는 `_MARKER_KEEP_PATTERN` 하나로 좁혀 두었다.

**채택안 v0 성적** (`--no-llm`). 측정 도중 **다른 작업이 코퍼스에 대량 적재를 했다.** 같은 main
코드로 잰 baseline이 ndcg 0.893 → 0.879, none_correct 1.000 → 0.667로 내려갔다. 그래서
**적재 이후 상태에서 baseline을 다시 찍어** 2분 간격으로 잰 쌍을 채택 근거로 삼는다.

| | baseline `cc07618e12ee…` (12:41:04) | **채택 `31515a5cba68…` (12:39:10)** |
|---|---|---|
| hit@1 | 0.814 | **0.829** |
| recall@5 | 0.886 | **0.886** |
| mrr | 0.879 | **0.886** |
| ndcg@10 | 0.879 | **0.882** |
| exhaustive_recall | 0.908 | **0.908** |
| count_match | 0.333 | **0.333** |
| none_correct | 0.667 | **0.667** |

paired: delta **+0.0036, 95% CI [-0.0021, +0.0115]** — 유의하지 않다. win/loss/tie **2/1/67**.
움직인 질의는 셋뿐이다: `v0-aggregation-002` 0.688→0.864, `v0-set_query-001` 0.342→0.494,
`v0-set_query-002` 0.936→**0.861**(유일한 회귀). 두 v1-draft 질의는 둘 다 1위로 회복했다.

적재 **이전** 상태에서 잰 쌍(`6f553efe3b22…` → `aa69d1c758ba…` 12:27:54)도 같은 그림이었다:
0.893 → 0.897, delta +0.0038 CI [-0.0013, +0.0112], 움직인 질의 동일한 셋. 위의 스트리핑
비교표(0.814/0.890 vs 0.857/0.897)도 적재 이전 수치다. **두 쌍 모두 A와 B가 같은 코퍼스
상태에서 측정됐으므로 delta 결론은 그대로 간다** — 절대값만 두 시점 사이에 평행이동했다.

#### 정정: baseline 하락의 원인은 NFC 마이그레이션이 아니라 **코퍼스 증가**였다

이 절은 처음에 하락을 "동시에 돌던 유니코드 NFC 마이그레이션 때문"이라고 적었다. **틀린 귀속이라
정정한다.** NFC 마이그레이션이 실제로 바꾼 행은 `documents.title` 4건 + `chunks.text` 4건이고
**전부 source_type=gmail**이다. 아래 두 문서는 google_drive라 대상이 아니었고 시점도 안 맞는다.

진짜 원인은 같은 시각 다른 작업의 **대량 적재**다. 전체 코퍼스 **263 → 326 문서**,
google_drive **7 → 70 문서**, 청크 **471 → 1426**. `eval_run.started_at`과
`documents.created_at`을 대조하면 계단이 정확히 맞는다:

| 시각 | 사건 | ndcg@10 |
|---|---|---|
| 12:28:24 | `perf-after-cold-2` | 0.893 |
| **12:35** | **문서 52건 생성** | |
| 12:35:00 | `perf-after` | 0.893 |
| 12:36:02 | `perf-after-final` | **0.885** |
| **12:37** | **문서 10건 생성** | |
| 12:37:11 | `baseline-recheck-HEAD` | **0.879** |
| 12:41:04 | `baseline-recheck` | 0.879 |

`none_correct`를 깨뜨린 두 질의의 오답 문서는 **그날 12:35에 새로 생성된 문서**다(기존 263건은
04:19 생성): `4bc1dbf1012e…` `seah_execution_plan_v3_1.html`,
`1aab373c4cd6…` `NH농협은행 AI Agent MVP · 진행 현황.html` — 둘 다 google_drive.

#### 그래서 드러난 것: **어휘 arm에 관련도 하한이 없다** (다음 과제)

`none_correct`가 깨진 두 질의는 **어휘 arm(`lexical_search`)에서만** 결과가 나왔다(나머지 arm은
전부 n=0, `kept_documents: 1`):

| 질의 | 매칭된 term | 걸린 문서 |
|---|---|---|
| `Kubernetes 클러스터 구성 가이드` | `클러스터` | KV 추출 정확도 표 |
| `연말정산 서류 제출 안내` | `제출` (`연말정산`은 0건) | OA 업무 매뉴얼 |

둘 다 질의에 실제로 답하는 문서가 아니다. **골든셋 라벨은 여전히 맞고, 검색이 진짜로 틀렸다.**

구조적 원인: `vector` arm에는 `KF_VECTOR_MIN_SCORE`(0.45) 관련도 하한이 있지만 **어휘 arm에는
대응물이 없다.** `구성`/`가이드`/`서류`/`제출`/`안내` 같은 흔한 한국어 단어가 per-term으로
매칭되고 `coverage_threshold`가 1건을 남긴다. **코퍼스가 커질수록 나빠진다** — 263문서에서
1.000이던 `none_correct`가 326문서에서 0.667이 됐다. 즉 지금의 어휘 arm은 **263문서 규모에서만
측정된 것이고 스케일하지 않는다.** 처방은 어휘 arm의 관련도 하한(term 희소성 가중 또는 점수
하한)이고, "남은 것" 2번과 같은 뿌리다.

#### 교훈: 페어드 비교에는 **코퍼스 상태**를 함께 기록해야 한다

v0 절대값은 **코드를 한 줄도 안 고쳐도** 코퍼스가 늘면 움직인다. `eval-ir --compare`가 이미
`index_snapshot differs between runs` 경고를 내주지만, 이번처럼 원인을 잘못 귀속하기 쉽다.
앞으로 실측을 기록할 때는 run_id와 함께 **그 시점의 문서 수/청크 수**를 적고, A와 B가 같은
코퍼스 상태인지 먼저 확인할 것. 다른 작업과 개발 DB를 공유하는 동안에는 baseline과 수정후를
**연달아** 찍는 것이 유일하게 믿을 수 있는 방법이다.

**정직하게 남기는 트레이드오프**: 어휘 힌트가 **맞았을 때**의 정밀도를 잃는다. v0 76개에서는
손실이 관측되지 않았지만(내려간 지표 없음), v0는 사람 검수 안 된 스모크셋이고 source_type
힌트가 맞는 질의가 많지 않다. `count`는 이 경로에서 SQL COUNT를 쓰지 않는다 — 믿지 못해
랭킹에서 뺀 필터로 센 숫자를 답으로 내면 돌려준 목록과 모순된다.

**버린 대안**: (a) `google_drive` 마커를 `['google_drive','gmail']`로 확장 — 이 두 질의만 낫고
슬랙/노션에서 같은 모양으로 재발한다. (b) "LLM이 확정한 제약만 하드 필터" — `source_types`는
애초에 LLM 보강 대상이 아니라(`LLM_ENRICHABLE_FIELDS`) provenance로는 갈리지 않는다. 그래서
provenance 대신 **술어의 종류**로 갈랐다. (c) 하드 필터 대신 재정렬(교집합 먼저, 나머지 뒤) —
`v1-draft-factoid-003`의 1위 정답이 드라이브 파일 4건 뒤 5위로 밀린다.

### 어휘 arm의 변별력 게이트 — 아홉 번째 실측 (2026-09-04)

일곱 번째 실측에서 드러난 "어휘 arm이 코퍼스 크기에 따라 무너진다"를 고친 것이다.
`fix/lexical-term-idf` (`fix/filter-no-overlap` 위에서 작업).

**측정 시점 코퍼스(전/후 동일)**: 326 문서 / 1891 청크 — gmail 59/268, google_drive 70/1426,
slack 197/197. 스윕 내내 적재를 멈춰 두고 쟀다.

#### 무엇이 문제였나

`_term_fallback`의 coverage 판정은 **매칭된 term의 개수만** 센다(`_coverage_threshold`).
4-term 질의면 임계값 3이다. 그런데 term의 변별력은 df로 천차만별이다:

| 질의 | 변별력 term (df) | 흔한 term (df) | 개수 기준 결과 |
|---|---|---|---|
| `Kubernetes 클러스터 구성 가이드` | Kubernetes **1**, 클러스터 **1** | 구성 26, 가이드 25 | 흔한 것 + 클러스터가 한 문서에 겹쳐 통과 → **오답** |
| `연말정산 서류 제출 안내` | 연말정산 **0** | 서류 7, 제출 6, 안내 13 | 흔한 것 3개 겹쳐 통과 → **오답** |
| `Terraform으로 인프라를 프로비저닝하는 절차` | Terraform으로 0, 프로비저닝하 0 | 인프라 2, 절차 14 | 2개뿐이라 3 미달 → 정답 (**운이 좋았을 뿐**) |

개수만 세면 **코퍼스가 커질수록 반드시 나빠진다** — 흔한 term끼리 우연히 겹치는 문서는
코퍼스 크기에 비례해 늘어난다. 지금 정답인 것들도 코퍼스가 더 커지면 깨진다.

점수 하한(`KF_VECTOR_MIN_SCORE`의 어휘판)은 쓸 수 없다. `search_chunks_lexical`의 score는
매칭 **티어 표시**(전체 substring=1.0 / word_similarity=raw)라 관련도 측정치가 아니다 —
`제출` 한 단어만 맞아도 1.000이 나온다.

#### 고른 방식: "질의에서 가장 희귀한 term(들)을 담고 있어야 한다"

`_required_terms`: df가 최소인 term 전부(`KF_TERM_RARE_DF_MAX`가 그 위에 얹는 절대 하한)를
문서가 담고 있어야 통과. 기존 개수 임계값과 **AND**로 병존한다(대체가 아니다 — 개수 기준을
빼고 게이트만 걸면 term arm이 남기는 노이즈 문서가 12 → 43건으로 는다).

동점을 **전부** 요구하는 것이 계약의 핵심이다. negative-001이 정확히 그 경우다: Kubernetes와
클러스터가 둘 다 df=1인데 **서로 다른 문서**에 있다. 하나만 요구하면 노이즈 문서가 살아남는다.

**버린 대안**:
- **IDF 가중 coverage**(`Σ idf(matched) / Σ idf(all) ≥ 임계값`). 분리가 안 된다. log-idf로
  계산하면 negative-001의 오답 문서가 0.684, `v0-factoid-001`의 **정답** 문서가 0.653이다 —
  오답이 정답보다 높아서 어떤 임계값으로도 가를 수 없다. `1/(df+1)` 가중으로 바꿔도 순서가
  같다. 구조적으로 "가장 희귀한 term을 담았는가"가 판정의 본질이고, 질량비는 그걸 흐린다.
- **"희귀 term을 하나라도 담으면 통과"**. negative-001의 오답 문서가 클러스터(df=1)를 담고
  있어 그대로 통과한다.
- **term별 히트 수를 df로 쓰기**(추가 질의 0회). 아래 절 참고 — 절단 때문에 판정이 뒤집힌다.

#### df를 어디서 얻나 — 절단이 판정을 뒤집는다

term마다 `lexical_search`를 이미 부르므로 그 히트에서 df를 세면 공짜다. **그런데 못 쓴다.**
`search_chunks_lexical`은 `LIMIT`(`_TERM_FETCH_LIMIT`=20)을 `is_substring DESC, sim DESC,
document_id` 순으로 자르기 때문에, 흔한 term일수록 상위 20청크가 **같은 문서 몇 개에 몰린다**:

| term | 절단 df | 실제 df |
|---|---|---|
| 인터뷰 | 3 | 37 |
| OCR | 1 | 34 |
| 애자일소다 | 1 | 33 |
| risk-agent | 4 | 51 |
| AI | 4 | 162 |

v0의 per_term 질의 26개 중 **7개에서 "가장 희귀한 term"의 신원이 뒤집혔다**. 판정 자체가
바뀌므로 별도 count가 필요하다 — `_term_document_frequency_sql`이 term 전부를 UNION ALL로 묶어
**읽기 전용 질의 한 번**에 센다(term당 한 번이 아니다). 값은 SQL df와 실측 df의 `max`다:
SQL은 substring 티어만 세어 fuzzy로만 걸린 term을 0으로 보고, 실측은 절단으로 흔한 term을 낮게
본다 — 둘 다 **과소평가만** 하므로 큰 쪽이 진실에 가깝고, "히트는 있는데 df=0이라 전부 기각"
이라는 모순이 생기지 않는다.

#### df=0을 어떻게 볼 것인가 — "전부 기각"으로 결정했다

`연말정산`처럼 코퍼스에 아예 없는 term이 최소 df면 어떤 문서도 그것을 담을 수 없다. 그래서
그 질의는 통째로 빈손이 된다. **의도한 동작이다** — 코퍼스에 없는 개념을 물었으면 "없다"가
정답이다. v0-null-001/002, v0-negative-002/003이 이 규칙으로 지켜진다.

반대 선택지("df=0 term은 판정에서 제외")도 실측했다: `none_correct`가 0.833에서 멈춘다
(null-001이 그대로 샌다). 재현율 쪽 손해는 작다(term arm이 남기는 정답 4 → 5, 노이즈 4 → 6).
즉 **null-001 한 건과 gold 한 건의 맞바꿈**이고, "없으면 없다고 말하기"를 필수 목표로 잡았으니
veto를 택했다.

veto의 진짜 위험은 오타/동의어로 df가 0이 되는 질의인데, `max(SQL df, 실측 df)`가 이걸
막는다: fuzzy 티어로라도 걸린 문서가 있으면 df ≥ 1이 되어 veto가 아니라 "그 문서를 요구"로
바뀐다(회귀 테스트 `test_term_document_frequency_takes_the_max_of_sql_and_observed`).

한편 형태소 부스러기가 df=0이 되는 경우는 실재한다(`값들`, `결정됐고`, `다이제스트`). 이때
그 질의의 term arm은 통째로 꺼진다. v0에서 그 대상은 3건이고 그중 term arm이 정답을 잡고 있던
것은 `v0-factoid-001` 하나인데, 그 질의는 `document_lookup`/`lexical` 전략이 identifier
`AGENTS.md`로 정답을 1위에 두고 있어 최종 지표가 그대로다(1.000). **term arm은 여러 arm 중
하나라 arm 하나의 재현율 손실이 그대로 지표 손실은 아니다** — 이번 트레이드오프가 싸게 먹힌
이유다.

#### 임계값 스윕 (`KF_TERM_RARE_DF_MAX`)

| cap | ndcg@10 | hit@1 | recall@5 | mrr | exhaustive_recall | count_match | none_correct | run_id |
|---|---|---|---|---|---|---|---|---|
| -1 (게이트 끔) | 0.882 | 0.829 | 0.886 | 0.886 | 0.908 | 0.333 | **0.667** | `cb88a08f8b684821bf70fc9204e1c433` |
| **0 (기본)** | 0.886 | 0.843 | 0.888 | 0.893 | 0.908 | 0.333 | **1.000** | `a96d75bccfe440c09b77d0abd251a4f7` |
| 1 | 0.886 | 0.843 | 0.888 | 0.893 | 0.908 | 0.333 | 1.000 | `906fd14029674c798bd8e5d857d71ffc` |
| 2 | 0.886 | 0.843 | 0.888 | 0.893 | 0.908 | 0.333 | 1.000 | `530f7d8ac6ff457793c2019f1daacf0f` |
| 3 | 0.892 | 0.857 | 0.888 | 0.900 | 0.908 | 0.333 | 1.000 | `6f3a1ba3a99c48dcbfd83f81aec38704` |
| 4 | 0.892 | 0.857 | 0.888 | 0.900 | 0.908 | 0.333 | 1.000 | `f07e3ed6165949598f9a1825bf6a9791` |
| 5 | 0.892 | 0.857 | 0.888 | 0.900 | 0.908 | 0.333 | 1.000 | `2b4a10bbeb194e70a2eae0f9127805a0` |
| 8 | 0.886 | 0.857 | **0.881** | 0.900 | **0.891** | 0.333 | 1.000 | `8fb669d1dd10457993532f498ac5728f` |

(cap=-1 run이 코드 변경 전 baseline `44d41c8934934c1bbdc59109563ae37b`과 소수점 셋째 자리까지
일치한다 — 게이트를 끄면 옛 동작으로 정확히 돌아간다는 sanity check. cap=6은 딜록으로 n=72라
표에서 뺐다.)

고원이 두 층이다: 0~2가 ndcg 0.886, 3~5가 0.892. 8이 절벽(recall@5·exhaustive_recall 하락).
**그런데 기본값은 위쪽 고원이 아니라 0으로 잡았다.** 이 노브는 **절대** df라 코퍼스가
작을수록 더 조인다 — 지금(326문서) cap=4는 질의 term의 평균 30%를 요구하지만, 코퍼스가 1/3로
작아지면 같은 4가 거의 모든 term을 요구해 재현율이 무너진 cap=8(평균 45% 요구)과 같은 자리에
선다. 이번 수정 자체가 "코퍼스 크기에 따라 무너지는 판정을 없앤다"는 것이므로, 질의 두 개짜리
+0.006(95% CI가 0을 포함)보다 크기 불변성을 택했다. cap=0은 최소 df만 따라가므로 코퍼스가
커지든 작아지든 "질의에서 가장 희귀한 term"이라는 의미가 유지된다.

| cap | 요구 term 비율(평균) | 요구 term 비율(최대) |
|---|---|---|
| 0~2 | 0.22~0.24 | 0.50~0.60 |
| 3~5 | 0.27~0.32 | 0.60~0.67 |
| 8 | 0.45 | 0.80 |

(구현상 cap 0과 1은 완전히 같은 규칙이다: 최소 df가 1 이상이면 하한 1이 아무 영향이 없고,
최소 df가 0이면 어차피 전부 기각이다. 표에서 두 줄이 같은 이유.)

#### 전/후 (기본값)

baseline `44d41c8934934c1bbdc59109563ae37b` → `efa882e7954c48ebbc493d3a92359b65`

| 지표 | 전 | 후 |
|---|---|---|
| ndcg@10 | 0.882 | **0.886** |
| hit@1 | 0.829 | **0.843** |
| recall@5 | 0.886 | **0.888** |
| mrr | 0.886 | **0.893** |
| exhaustive_recall | 0.908 | 0.908 |
| count_match | 0.333 | 0.333 |
| **none_correct** | **0.667** | **1.000** |

페어드 비교(`eval-ir --compare`, n=70 — none 질의 6개는 compare 대상이 아니다):

| 지표 | delta (B−A) | 95% CI | win/loss/tie |
|---|---|---|---|
| ndcg@10 | +0.0041 | [+0.0000, +0.0114] | 2/0/68 |
| recall@5 | +0.0014 | [+0.0000, +0.0043] | 1/0/69 |
| hit@1 | +0.0143 | [+0.0000, +0.0429] | 1/0/69 |
| mrr | +0.0071 | [+0.0000, +0.0214] | 1/0/69 |

전부 CI 하한이 0이라 **통계적으로 유의하지 않다**(움직인 질의가 70개 중 1~2개뿐이다).
필수 목표였던 `none_correct` 0.667 → 1.000은 negative/null 각각 6건 중 2건이 고쳐진 것이다.

**손해 본 유형은 없다** — 모든 지표에서 loss가 0이다. 이득은 `v0-multihop-001`
(ndcg 0.387 → 0.613, hit@1 0 → 1: 흔한 term만 겹친 문서가 1위에서 빠졌다),
`v0-set_query-002`(recall@5 0.400 → 0.500), `v0-exact_match` 소폭. 유형별로는
cross_document_comparison(ndcg +0.057, hit@1 +0.250), multihop(+0.057, +0.250),
exact_match(+0.007), set_query(+0.004)만 움직였고 나머지 16개 유형은 전부 tie다.

**정직하게 남기는 트레이드오프**: term arm 자체의 재현율은 줄었다. 오프라인 시뮬레이션 기준
term arm이 남기던 정답 문서 7 → 4, 노이즈 12 → 4다. 줄어든 정답 3건(`v0-factoid-001`,
`v0-set_query-002`·`v0-sequence-002`의 정답 각 1건)은 다른 arm이 메워서 최종 지표에
안 나타났지만(해당 유형 전부 tie 또는 개선), **arm이 하나뿐인
질의에서 같은 일이 벌어지면 그때는 지표로 나타난다.** v0는 76개짜리 미검수 스모크셋이라
이 손실을 잡아낼 해상도가 없다 — v1 검수 골든셋에서 다시 재야 한다.

#### 어휘 arm 1단계(`lexical_search#semantic`)에는 같은 문제가 없다

이번 2건에서 그쪽이 0히트라 안 걸린 게 아니라 **구조적으로 안 걸린다.**
`search_chunks_lexical`의 3티어 중 1·2티어는 "질의 전체가 한 청크의 substring" / "질의의 모든
토큰(`_MAX_QUERY_TOKENS`=8까지)이 **한 청크 안에** 전부 있음"이라, 흔한 term만으로는 성립할 수
없다(희귀 term도 반드시 포함해야 통과한다). 3티어는 질의 전체에 대한 `word_similarity ≥ 0.5`로
개수가 아니라 **관련도 하한**이고, 그 값은 코퍼스 크기와 무관하다. 즉 "코퍼스가 커지면 우연한
겹침이 늘어난다"는 메커니즘이 여기엔 적용되지 않는다. 실측으로도 v0의 none 질의 6개 전부
이 경로가 `limit=200`에서도 title/body 0히트다(그래서 term 분해로 내려갔다). 그래서 게이트를
1단계로 확장하지 않았다. 남는 취약점은 토큰 8개 상한뿐인데(9번째 이후 토큰은 요구되지 않는다),
"8개 토큰이 **한 청크 안에** 전부"는 문서 단위 2/3 겹침과는 비교가 안 되게 강한 조건이다.

#### 주의: 랭킹 노브는 `config_hash`에 안 들어간다

`eval-ir --compare`가 "both runs share config_hash" 경고를 낸다. `config_hash`는 CLI 인자
(provider/model/limit/strategies/k)만 해싱하고 `KF_VECTOR_MIN_SCORE`/`KF_TERM_*` 같은 랭킹
설정은 포함하지 않는다. 랭킹 노브 스윕에서는 이 경고를 무시하되, **label에 값을 박아 두는 것이
유일한 기록**이다(이번 스윕은 `raredf-<cap>`).

### 남은 것 (우선순위 순)

진단은 `eval_run_query.steps` 실측 기준이다.

1. **임베딩 없는 상태를 최적화하고 있다는 것** — 76/76 질의에서 `vector_search`가 0히트다.
   `OPENAI_API_KEY` + `KF_EMBEDDING_SOURCE=chunk`로 채우기 전까지 위 튜닝은 전부 degrade
   경로 최적화다. 어휘 겹침이 0인 질의(`v0-latest-003`, `v0-relative_temporal-001`
   "농협"→`nonghyup.com`, `v0-metadata_filter-003`)는 원리적으로 못 푼다.
2. ~~**term별 희소성 가중 / 어휘 arm의 관련도 하한**~~ — **아홉 번째 실측에서 해결**
   (`_required_terms`, `KF_TERM_RARE_DF_MAX`). `none_correct` 0.667 → 1.000, 내려간 지표 없음.
   남은 것: term arm 자체의 재현율 손실(정답 7 → 4)이 다른 arm에 가려져 v0에서는 안 보인다 —
   v1 검수셋에서 다시 재야 한다. 그리고 df가 이제 substring 티어 기준이라, 형태소 부스러기가
   df=0이 되어 term arm이 통째로 꺼지는 질의가 있다(`값들`, `결정됐고`) — `keyword_terms`의
   조사/접미사 처리를 손보면 같이 해결된다.
3. **metadata_filter 교집합이 (거의) 무조건적** — 키워드 히트가 1건이라도 있으면 `filter_only`가
   `intersected`로 바뀐다. `filter_only` 경로가 baseline 5건 → 지금 0건. 신뢰도 낮은 term
   후보로 교집합을 걸지 말고 필터 집합을 랭킹 base로 두고 키워드를 가산점으로 쓰는 편이 안전하다
   (`v0-set_query-002`, `v0-relative_temporal-001`). **여덟 번째 실측에서 절반만 해결했다** —
   어휘 힌트뿐인 필터는 이제 교집합을 걸지 않지만(`source_hint_not_applied`), 구조적 술어가
   섞인 필터는 여전히 무조건 교집합이다. 진짜 처방은 필터를 RRF에 가산점으로 섞는 것이고,
   그건 `rerank.py` 쪽 변경이다.
4. ~~**fallback 경로에서 anchor가 안 잡힌다**~~ — 여섯 번째 실측에서 `_claim_anchor` 공유로
   해결. 남은 것은 **RRF가 모든 소스를 1표로 본다**는 쪽이다. 일곱 번째 실측이 팔별 값을
   따로 재 줬으니(arm 0.663 vs vector 0.477) 소스 가중을 근거 있게 논의할 수 있다.
5. **슬랙 문서 제목이 197/197 채널 ID다**(`#C0BGQEQG1L6 2026-09-01`). 수집기가
   `sources.metadata.channel_name`을 안 채운다. 골든셋 정답 문서 90개 중 48개가 슬랙이라,
   제목 기반 조회/anchor 경로가 정답 절반 이상에 구조적으로 닿지 않는다 — 4번의 전제조건.
6. **`count`가 `len(document_ids)`다** — 어휘 조건 기반 count를 SQL로 내려야 골든셋의 count
   정의와 같은 것을 센다.
7. **`relations` 문서 간 edge 0행 / `chunks.parent_chunk_id` 전부 NULL** — multihop·parent_child
   유형은 지금 데이터로 실제 테스트가 안 된다. parent_child 1.000은 "문서=부모"로 우회한 결과다.
8. **골든셋 v0 자체** — `v0-latest-003`은 정답 문서 본문에 질의의 "3차"도 "공지"도 없다(사람의
   해석이 개입한 정답). v1 검수 때 질의문 수정 또는 제외 대상.

### 동시 실행 주의

`PostgresStore`는 커넥션 하나를 공유하고 `execute_readonly_query`만 명시적 트랜잭션을 연다.
`eval-ir --concurrency`가 1을 넘으면 이게 겹쳐 `OutOfOrderTransactionNesting`이 났었다
(첫 baseline에서 76개 중 20개 실패). 지금은 그 메서드가 `asyncio.Lock`으로 직렬화한다.
**앞으로 `PostgresStore`에 트랜잭션을 여는 메서드를 추가하면 같은 락을 써야 한다.**

별개로, `eval-ir`를 돌리는 동안 같은 DB에 `pytest`를 돌리면(테스트가 DDL을 친다) 서로
`DeadlockDetected`로 죽는다. 스윕과 테스트는 겹쳐 돌리지 마라.

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
3. **(C) 골드셋 기반 Recall/Precision/MRR/NDCG — 스키마·v0·지표·`eval-ir`/`eval-gen` CLI
   구현됨, v1 사람 검수가 다음.** `evaluation/golden.py`/`ir_metrics.py`/`ir_runner.py`/`ir_compare.py`,
   `eval_run`/`eval_run_query` 테이블, `knowledge-forge eval-ir`(실행·`--compare`·`--list`)까지
   있다. v0 baseline run은 남겼고, `eval-gen`이 낸 `v1-draft.json`을 사람이 검수해 v1으로
   굳히는 것이 다음이다.
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

## 골든셋 v1 — 세 갈래 통합 + 사람 검수 (2026-09-05)

`v0.json`(손 작성 76) / `v1-draft.json`(eval-gen 1차 18) / `v2-draft.json`(eval-gen 2차 gdrive 40)로
쪼개져 있던 골든셋을 **`tests/eval/golden/v1.json` 하나(95개)로 통합**했다. `v0.json`은 15개 run의
기준선이라 **그대로 둔다**(불변). `compare_runs`가 `golden_version`이 다르면 `ValueError`를 던지는
그대로, **v0 점수와 v1 점수는 직접 비교할 수 없다** — 질의 모집단이 다르다.

### 계보를 파일 안에 남긴 방법 — `src/`를 건드리지 않고

`GoldenQuery.origin`은 `manual|synthetic|feedback`만 받는 pydantic 필드라 "어느 파일에서 왔는가"를
여기에 넣을 수 없다. 그래서:

- **원래 id를 그대로 쓴다.** v1의 질의 id는 `v0-factoid-001` / `v1-draft-factoid-003` /
  `v2-draft-parent_child-005`처럼 원본 그대로다(세 파일의 id 공간이 이미 겹치지 않는다).
  id만 봐도 출신을 알 수 있고, 과거 run의 `eval_run_query.query_id`와도 이어진다.
- **`origin_set` / `original_id`를 추가 필드로 넣는다.** pydantic 기본값이 `extra="ignore"`라
  로더가 조용히 무시하고 파일에는 남는다 — 스키마를 고치지 않고 "손 작성 대 합성"을 갈라 보는
  가장 싼 방법이다. 값은 `v0-handwritten` / `evalgen-v1` / `evalgen-v2`.
- `origin`은 스키마가 허용하는 `manual`/`synthetic` 그대로 둔다.

### 검수에서 세운 라벨 규칙 — "산문으로 답이 읽히는 문서만"

라벨 누락을 고치다 보면 "이 문서도 정답인가"를 계속 판정해야 한다. 규칙을 하나로 못박았다:
**문서를 열었을 때 산문이나 표로 답이 읽히면 정답, 아니면 아니다.**
구체적으로 `projectintel-development-handover.pptx`(`7f30ecf4…`)는 청크 437개에 **중앙값 7토큰**,
189개가 5토큰 이하인 슬라이드 조각으로 파싱돼 있다(VLM 미적용). "PRJ-* canonical" 같은 3토큰
파편에 답의 토큰이 들어 있다고 정답 문서로 세면 파서 결함을 골든셋에 새기는 셈이라,
**원래 라벨에 있던 경우만 유지하고 검수 중에는 새로 추가하지 않았다.** 코퍼스 전체로는
2,856청크 중 605개(21%)가 5토큰 이하다.

### 무엇을 걷어냈나

| 출처 | 원본 | 채택 | 비율 |
|---|---|---|---|
| v0 (손 작성) | 76 | **74** | 97% |
| v1-draft (eval-gen 1차) | 18 | **11** | 61% |
| v2-draft (eval-gen 2차, gdrive) | 40 | **10** | 25% |
| 합 | 134 | **95** | 71% |

**v0에서 뺀 2건** — 둘 다 "사람 해석이 정답을 만들고 있던" 경우다.

- `v0-multihop-001` "OA 업무도우미 과제의 개발환경이 2차 인터뷰 후 어떻게 결정됐고…":
  hop1(메일)은 *OA 업무도우미*가 외부개발로 결정됐다고 하고, hop2(슬랙)는 *6번 과제*를 외부
  개발로 바꾼다고 한다. **"6번 과제 = OA 업무도우미"라고 적은 문서가 코퍼스에 없다.**
  검색기도 judge도 확인할 수 없는 연결이라 뺐다.
- `v0-exact_match-001` `F0BUMH30V40`: 원시 슬랙 파일 id. 사람이 칠 질의가 아니고, 대상 문서는
  그 id를 언급하는 한 줄짜리 메시지다.

**v0에서 고친 질의 1건**

- `v0-latest-003` "가장 최근의 3차 인터뷰 일정 변경 공지" → **"인터뷰 일정이 변경된 가장 최근
  공지는?"**. 정답 문서(`b2bf0036…`)에는 "3차"도 "공지"도 없다 — 그 문서는 "내일 16번 과제 인터뷰
  시간이 16시에서 15시로 변경"이고, 정작 "3차 인터뷰"라고 적힌 `5fe63f02…`는 일정 *변경* 공지가
  아니다. baseline에서 이 질의만 ndcg@10=0.000이었던 이유다. "3차"를 빼면 후보가
  09-01 PF팀 연기(`22b17a6b`)와 09-02 시간 변경(`b2bf0036`) 둘이 되어 `latest` 판정이 성립한다.

**합성 질의에서 뺀 것 — 세 가지 실패 유형**

1. *앵커 없는 질의*(가장 많다). 질의만 보고 사람이 어느 문서인지 특정할 수 없다.
   `기존 문서를 대체할 때 삭제하는 대신 어떤 표시를 우선해야 하는가?`,
   `MR/PR 제목과 본문은 어떤 언어로 작성해야 하나요?`,
   `PRD/설계/운영 기준 변경 전 확인 문서 순서는?`,
   `Project Structure 문서에서 정의한 주요 실행 경계 디렉토리 목록은?`
   (마지막 것은 "Project Structure"가 문서가 아니라 AGENTS.md 안의 **절 제목**이다).
2. *청크 문장 되묻기*. `copied_from_chunk`(최장 공통 부분 문자열 25자)를 빠져나가는 부류다.
   `바이브 코딩은 AI에게 크게 맡기는 방식인가요?`(청크 첫 문장을 부정문으로 뒤집음),
   `ProjectIntel handover 문서에는 secret 원문이 포함되어 있는가?`("이 문서는 secret 원문을 담지
   않는다"의 예/아니오 뒤집기 — 게다가 그 문장은 두 문서에 그대로 있다),
   `project-intel Codex 기준 가이드 파일의 Scope는?`(168자 청크가 질의와 같은 말로 답한다).
3. *원시 채널 id 앵커*. `Slack 채널 #C0BSW8Y2AD9에서 …`, `Slack #C0BT6M412N9 채널에서 …`.
   `#C0BSW8Y2AD9`로 시작하는 제목이 35개, 같은 날짜까지 같은 제목이 7개다. 사람이 칠 질의가
   아니면서 라벨도 틀린다. 한 건(`v1-draft-conversation_thread-003`)만 청크 안의 고유 표현으로
   **다시 써서** 살렸다: `금요일 4시 OA 회의는 어디서 진행하나?`(`선 결제`/`4시 OA`는 코퍼스에서
   유일하다).

**v2-draft는 문서당 상한을 걸었다.** 40개의 정답 문서가 5개뿐이고 34개가 문서 2개
(`AGENTS.md`, `projectintel-handover-guide.md`)에 몰려 있어, 그대로 넣으면 v1이 "그 두 문서를
얼마나 잘 찾나"를 재게 된다. 문서당 상한 3으로 잘라 10개만 채택했다.

### 라벨 누락 9건 수정 — 이게 검수에서 가장 값이 나가는 부분

binary relevance라 **정답인데 라벨이 안 된 문서가 상위에 올라오면 precision@10/ndcg@10이 부당하게
깎인다.** 실제로 9건에서 정답이 빠져 있었고, 6건은 **코퍼스가 263→358로 커지기 전부터 이미 틀려
있었다**(성장 탓이 아니다).

| 질의 | 이전 | 이후 | 사유 |
|---|---|---|---|
| `v1-draft-factoid-003` | 1 | **3** | 제목·본문이 같고 요청자/일시만 다른 공유 요청 메일 3건(`156dea7a`/`7b51f2ed`/`8595924b`) |
| `v1-draft-temporal-001` | 1 | **3** | 같은 3건 위에 있던 또 다른 질의 — 원래 지적에 없었다 |
| `v0-metadata_filter-003` | 1 | **12**, exhaustive | "gdrive pptx 파일" — 263 시절엔 1건, 지금 12건. **성장으로 깨진 가장 큰 라벨** |
| `v0-factoid-007` | 1 | **3** | "canonical project id 형식" — AGENTS.md와 신규 운영 가이드 PDF도 명시 |
| `v0-factoid-009` | 1 | **3** | "업로드 일자 2026-07-20"이 담긴 "현재 업로드 기록" 표가 세 문서에 동일 |
| `v0-entity_centric-001` | 7 | **8** | 신규 "프로젝트 필요 인력 정리 파일_260623.xlsx"에 동양생명 MM 배분표 |
| `v0-entity_centric-002` | 4 | **6** | 신규 계약 스프레드시트 2건에 광주은행 RIMS 계약 행 |
| `v0-ambiguous-003` | 4 | **5** | 신규 "관리자 운영 가이드.dc.html"이 스스로를 "인수인계 가이드"라고 적는다 |
| `v0-multihop-003` | 2 | **3** | hop1(직책 표준화)에 해당하는 슬랙 문서 `83a905e7`("김대길 계장이 책임으로") 누락 |

여기에 합성 질의 6건(`v1-draft-factoid-005/009`, `v2-draft-factoid-010/012/013/016`,
`v2-draft-exact_match-001`)의 라벨도 1→2~3건으로 넓혔다.

### 유형 재분류 5건

`exact_match`는 "파일 id·repo URL·호스트명처럼 **질의 자체가 리터럴**"인 유형인데,
`…Google Sheets 링크는?` / `…환경변수는 무엇인가?` / `…Drive 폴더 URL은?`은 리터럴이 답이지
질의가 아니다. 3건을 `factoid`/`specific_document_qa`로 내렸다. `v1-draft`의 `temporal` 3건도
"메일 헤더 날짜 되묻기"라 `factoid`로 내리고 `temporal`은 부라벨로만 남겼다 — 그대로 두면
temporal 버킷이 실제보다 쉬워 보인다.

### staleness — `source_hash`는 통과했지만 그게 안전하다는 뜻이 아니다

134개 전부에 대해 `check_staleness`를 돌려 **stale 0건**(missing 0 / changed 0 / unhashed 0).
코퍼스가 263→358로 커진 방식이 **기존 문서 수정이 아니라 gdrive 문서 95개 추가**뿐이라
정답 문서들의 `sources.content_hash`가 하나도 안 바뀌었기 때문이다.

**그런데 실제로는 4건이 깨져 있었다**(위 표의 metadata_filter-003 / entity_centric-001·002 /
ambiguous-003). `check_staleness`는 *라벨된 정답 문서*만 해시하므로,
**"새로 들어온 문서가 정답 집합에 들어가야 하는가"를 원리적으로 볼 수 없다.**
exhaustive/count/none 질의에는 해시 비교가 아니라 **술어 재도출**이 필요하다 —
`notes`에 적어 둔 SQL을 다시 돌리는 것이 유일하게 신뢰할 수 있는 검사다.
(aggregation 3건은 재실행 결과 6/23/11로 변동 없었고, negative·null 6건의 금칙어
kubernetes/terraform/kafka/연말정산/블록체인/특허는 358문서에서도 전부 0건이었다.)

### 세션 중에 코퍼스가 바뀌었다 — 공유 DB 사고 재발

검수 도중 **슬랙 문서 197개의 `documents.title`이 통째로 다시 쓰였다**
(`#C0BSW8Y2AD9 2026-09-02` → `#proj_2026-038-0_nh농협은행_mvp 2026-09-02`). 다른 세션의 백필이다.
`sources.content_hash`도 `documents.updated_at`도 안 바뀌어서 **`check_staleness`도 `index_snapshot`도
이 변화를 잡지 못한다.** 제목은 pg_trgm/렉시컬 arm의 입력이므로 이건 검색 결과를 바꾸는 변화다.
확인 삼아 같은 코드(da64fac)로 v0을 다시 돌렸다:

| run_id | 시점 | ndcg@10 |
|---|---|---|
| `c729471ccea744468dad78ec9c43e82d` | 제목 백필 **전** | 0.8516 |
| `a51c0681b8b14e17b68b58362719747d` | 제목 백필 **후** | 0.8490 |

−0.0026이라 이번엔 실질적 영향이 없었다. 하지만 **운이 좋았던 것이지 안전한 게 아니다** —
`eval_run`에 코퍼스 지문(예: `max(documents.updated_at)` + 제목 해시)이 없어서, 이 −0.0026이
코드 변경 탓인지 코퍼스 변조 탓인지 기록만으로는 영원히 구분할 수 없다.
`eval-provenance-and-corpus-hazards`에 적힌 것과 같은 부류의 결함이다.

### v1 첫 절대값 baseline (2026-09-05)

```bash
knowledge-forge eval-ir --golden tests/eval/golden/v1.json --no-llm --label v1-baseline-2026-09-05
```

**`run_id=9eac82dcd2724727b95364dd6d83dabe`**, `git_commit=26c3527a3f831535fcca798abc8a09c4f3cefde9`
(깨끗한 트리), 코퍼스 documents 358 / chunks 2856 / chunk 임베딩 2856, stale 0 / error 0.

```
overall: n=95 hit@1=0.742 recall@5=0.892 mrr=0.834 ndcg@10=0.851
         exhaustive_recall=0.894 count_match=0.333 none_correct=1.000
```

| 잘 되는 유형 | ndcg@10 | 안 되는 유형 | ndcg@10 |
|---|---|---|---|
| conversation_thread / cross_doc / long_range_context / relative_temporal | 1.000 | **latest** | 0.500 |
| specific_document_qa | 0.972 | **multihop** | 0.656 |
| aggregation | 0.888 | **navigational** | 0.742 |
| parent_child | 0.875 | **temporal** | 0.741 |
| factoid | 0.874 | **sequence** | 0.742 |

**같은 시점 v0은 0.849(`a51c0681…`, n=76)로 v1의 0.851과 거의 같지만, 이 둘을 "성능이 같다"로
읽으면 안 된다.** 질의 모집단이 다르고(v1은 v0의 실패 질의 2건을 빼고 합성 질의 21개를 더했다),
난이도 구성도 다르다. v1이 앞으로의 기준선이고, v0은 과거 15개 run과의 연속성 때문에 남긴다.

### 이 세트가 감지할 수 있는 최소 효과 크기 — 95개로는 부족하다

v1 baseline의 질의별 ndcg@10은 **n=89(none 유형 6개는 ndcg가 없다), 평균 0.851, sd 0.257**,
단일 run 95% 마진 **±0.054**. A/B는 짝지어 보므로 필요한 건 *paired* sd인데, 이건 변경이
몇 개 질의를 움직이느냐에 따라 크게 다르다. 과거 v0 run 쌍에서 실측:

| 비교 쌍 | 움직인 질의 | paired sd | +0.03 감지에 필요한 n | +0.02에 필요한 n |
|---|---|---|---|---|
| `raredf-*` 이웃 파라미터 (1~3개 이동) | 1~3 / 70 | 0.028~0.052 | 7~23 | 15~53 |
| vfloor 0.50 → 0.42 (실전형 튜닝) | 13 / 70 | **0.176** | **270** | 608 |
| hybrid → vfloor 0.45 | 14 / 70 | 0.172 | 259 | 582 |
| constraints-fixes → hybrid-keyword-arm (전략 변경) | 24 / 70 | 0.269 | 631 | 1420 |

즉 **n=95에서 80% power로 감지 가능한 최소 효과는 paired sd 0.17 기준 약 +0.05**이고,
+0.03을 감지하려면 **250~300개**, +0.02는 **550개 이상**이 필요하다.
현실적 결론: **v1로는 "0.05 이상 움직이는 변경"만 신뢰할 수 있다.** 그보다 작은 delta는
`bootstrap_ci`와 win/loss/tie를 함께 보고 "유의하지 않음"으로 다뤄야 하며, 숫자가 올랐다는
이유만으로 채택하면 안 된다.

### 유형·출신 분포 (95개, `primary_type` 기준)

| 유형 | v0-handwritten | evalgen-v1 | evalgen-v2 | 합 |
|---|---|---|---|---|
| factoid | 10 | 9 | 8 | **27** |
| specific_document_qa | 4 | 1 | 1 | 6 |
| navigational | 4 | 0 | 0 | 4 |
| metadata_filter | 4 | 0 | 0 | 4 |
| conversation_thread | 3 | 1 | 0 | 4 |
| parent_child | 3 | 0 | 1 | 4 |
| aggregation / ambiguous / cross_document_comparison / entity_centric / latest / long_range_context / negative / null / relative_temporal / sequence / set_query / temporal / version / whole_document_summary | 3 each | 0 | 0 | 3 each |
| exact_match | 2 | 0 | 0 | 2 |
| multihop | 2 | 0 | 0 | 2 |

**22개 유형이 전부 채워졌다.** 청크 하나로는 만들 수 없는 9개 유형
(aggregation/set_query/negative/null/multihop/cross_document_comparison/relative_temporal/
latest/ambiguous)은 예상대로 **전부 v0에서 왔다** — 합성 생성기는 여기에 한 건도 기여하지 못했고,
이건 `gen.py::SINGLE_CHUNK_TYPES`의 구조적 한계라 파라미터로 고쳐지지 않는다.

정답 문서는 **106개**(v0 단독 90개에서 늘었다). 다만 편중은 남는다:
`projectintel-handover-guide.md` 17회(18%), `AGENTS.md` 13회(14%). 개발 DB에 산문 gdrive
문서가 그만큼 적어서 생긴 구조적 문제이지 검수로 더 줄일 수 없다.

### 남은 구멍

1. **신규 gdrive 95개 문서에 `knowledge_objects`가 0건이다.** 청크 임베딩은 2856/2856로 백필됐지만
   KO는 안 만들어졌다(기존 gdrive 7개에 1801개, gmail 1372, slack 1934). KO를 읽는 arm에게
   이 코퍼스는 여전히 263문서다. 이 비대칭이 위 라벨 수정 전부보다 절대값을 더 크게 움직인다.
2. **`project_id`가 일관성이 없다.** gdrive 102개 전부가 `gdrive-live-check`(테스트용 id처럼 보인다),
   gmail은 `default`, slack은 12개 `PRJ-*`에 흩어져 있다. project 스코프 필터를 켜면 질의마다
   코퍼스 절반이 조용히 사라진다.
3. **content_hash가 같은 중복 문서가 14그룹 65건**(슬랙 봇 공지가 채널 8~10개에 fan-out).
   같은 텍스트끼리의 순위는 임의라 그 질의의 nDCG는 부분적으로 노이즈고, dedup 레이어를 넣으면
   recall이 무너진다. 합성 생성기에 "정답 문서가 다른 문서와 content_hash를 공유하면 생성 금지"
   가드가 필요하다.
4. **청크가 0개인 문서 3건**(`ASME B36.10 …(OCR).pdf`, `si-onboarding-prototype.html`,
   `포항공장 생산 CAPA CHART(2021)_PM53 업데이트.xlsx`)은 라벨이 뭐라 하든 검색 불가. 실효 코퍼스는 355다.
5. **`.md`가 `text/plain`으로 들어왔다.** 기존 handover 마크다운 6개는 `text/markdown`인데 신규
   13개는 `text/plain`이다. `v0-set_query-001`("마크다운 문서 전부")의 정답이 유지되는 건
   검색 가능한 성질 때문이 아니라 **인제스트 불일치 덕분**이다.
6. **`v0-whole_document_summary-001`과 `v0-long_range_context-002`는 사실상 같은 질의**다
   (둘 다 같은 pptx 요약, 같은 단일 라벨). 완전 상관이라 유효 표본을 부풀린다. 유형 버킷이
   각각 3개뿐이라 이번엔 남겼다.
7. **`count_match`가 0.333에서 안 움직인다**(aggregation 3건 중 1건). retriever가 개수를
   예측하지 않으므로 사실상 상수다 — 지표로 쓰려면 집계 경로가 먼저 필요하다.

### 다음 단계

1. **eval-gen을 넓어진 gdrive로 다시 돌린다.** `--min-tokens 80` 후보를 가진 gdrive 문서가
   **7개 → 83개**(후보 청크 32 → 827)로 늘었다. v2-draft를 만들 때 `--max-per-document 13`까지
   올려야 했던 이유가 사라졌으므로, 이제 기본값 `--max-per-document 2`로 문서 40~80개에 퍼진
   초안을 뽑을 수 있다. **v2-draft의 실패(정답 문서 5개)는 생성기 문제가 아니라 그때 코퍼스가
   얕았던 문제였다.**
2. **표본을 250개 이상으로.** 위 계산대로 +0.03을 감지하려면 그만큼 필요하다. 늘리는 경로는
   (1)의 재생성 + 사람 검수이지, 지금 있는 질의를 재활용하는 게 아니다.
3. **`check_staleness`를 술어 재도출로 교체.** exhaustive/count/none 질의의 `notes`에 있는 SQL을
   실행해 정답 집합을 다시 뽑고 파일과 대조하는 검사가 필요하다. 지금 방식은 이번에 4건을 놓쳤다.
4. **`eval_run`에 코퍼스 지문을 기록.** `index_snapshot`의 행 수만으로는 제목 백필 같은
   "행 수는 그대로인데 내용이 바뀐" 변화를 못 잡는다.
## 부록: 제목 토큰 집합 tier 측정 (2026-09-05, v0 / documents 358)

`storage/title_match.py`(설계 근거는 `search-architecture.md`) 도입 전후를 **같은 worktree
에서 연달아** 돌린 페어드 비교. 코퍼스가 263→358로 바뀌었으므로 그 이전 run과는 비교하지
않았다.

| run_id | 라벨 | ndcg@10 | hit@1 |
|---|---|---|---|
| `0ae2d4e99ad04fbda15e0db7f1944f4c` | baseline (토큰 tier 없음) | 0.852 | 0.729 |
| `5b73394e8e014767a9218440a5ae4d57` | treatment (집합 1.0 + 부분집합 0.8 + 부분일치) | 0.859 | 0.743 |
| `e8849c6dc0b541a0866b45a0c50ee903` | ablation (부분일치 tier 끔) | 0.863 | 0.757 |
| `6e79054e7ada45daa52c9742c85eec56` | baseline 재실행 (코드 동일) | 0.850 | 0.771 |
| `03dc9150838e4e37abba1aba0952b2d6` | treatment 재실행 (커밋 4b842b2, dirty 아님) | 0.856 | 0.743 |

`--compare A B`: delta +0.0071, 95% CI [+0.0000, +0.0214], **win/loss/tie = 1/0/69**.
움직인 질의는 `v0-navigational-004`(`"Dev Handover README 파일"`) 하나뿐: **0.500 → 1.000**.
집계 delta는 노이즈 바닥(±0.056) 안이라 "지표가 올랐다"고 주장하지 않는다 - 주장하는 것은
"의도한 질의 하나가 결정적으로 고쳐졌고 나머지 69개는 그대로다"이다.

score 1.0 판정 개수도 직접 셌다. 골든셋 76개 질의가 실제로 제목 검색에 던지는 문자열
94개(제약 추출의 `document_reference`/`identifiers`/`quoted_phrases`/`semantic_query`)에
대해 **1.0 히트 22건 → 23건**. 늘어난 딱 한 건이 위의 `README_ProjectIntel_Dev_Handover.md`다.
`v0-ambiguous-002`(`"README"`)는 1.0이 두 건 그대로라 여전히 ambiguous로 남는다(anchor
안 잡힘) - 이게 이 설계에서 가장 지키고 싶었던 성질이다.

다섯 run의 질의별 ndcg@10을 세로로 보면 "코드가 바꾼 것"과 "그냥 흔들린 것"이 갈린다:

| 질의 | base | treat | ablation | base 재실행 | treat 재실행 |
|---|---|---|---|---|---|
| `v0-navigational-004` | 0.500 | **1.000** | **1.000** | 0.500 | **1.000** |
| `v0-ambiguous-002` | 0.651 | 0.651 | 0.651 | 0.651 | 0.651 |
| `v0-sequence-001` | 0.177 | 0.177 | 0.501 | 0.501 | 0.000 |

첫 줄은 코드를 따라 움직이고(주장할 수 있는 것), 둘째 줄은 지키려던 성질이 지켜졌다는 증거,
셋째 줄은 코드와 무관한 churn이다(아래).

### 부수 발견: `eval-ir`은 결정적이지 않다

위 표의 마지막 행이 그 증거다 - **코드가 완전히 같은 두 baseline run이 8개 질의에서
갈렸다**(최대 ±0.63; `v0-whole_document_summary-003` 0.631→0.000, `v0-multihop-002`
1.000→0.613, `v0-exact_match-001/002` 0.631→1.000). 질의 임베딩은 결정적임을 확인했으므로
(같은 문자열 두 번 호출 → 벡터 완전 일치), 원인은 그쪽이 아니라 **동점 처리**로 보인다:
코퍼스에 제목·본문이 사실상 같은 문서가 여럿 있어(`"…에 대한 공유 요청"` 계열) 거리/점수가
정확히 같은 행들이 나오는데, `ORDER BY`에 결정적인 마지막 tiebreak가 없으면 어느 행이
먼저 나올지가 실행마다 달라진다.

측정 규칙에 주는 함의:

- **단일 run 페어드 비교에서 8/76 정도의 질의 churn은 코드와 무관하게 생길 수 있다.**
  집계 delta뿐 아니라 **어떤 질의가 왜 바뀌었는지**를 반드시 같이 봐야 한다.
- 확인 방법은 싸다: 같은 코드로 baseline을 두 번 돌려 그 사이의 win/loss를 세면 그 시점의
  churn 바닥이 나온다. 위 비교에서 `v0-navigational-004`가 두 baseline에서 모두 0.500,
  두 treatment에서 모두 1.000이었던 것이 "이건 노이즈가 아니다"의 근거다.
- 근본 처방은 랭킹 SQL의 `ORDER BY` 끝에 결정적 tiebreak(`document_id` 등)를 붙이는 것이다.
  `search_documents_by_title`(`d.title, d.document_id`)과 `search_chunks_lexical`
  (`c.document_id, c.sequence_index`)에는 이미 있는데, **`search_by_embedding`은
  `ORDER BY e.embedding <=> %s`뿐이라 거리가 같은 행들의 순서가 정해져 있지 않다**
  (postgres_store.py:1097). 이 파일은 지금 다른 작업자가 잡고 있어 손대지 않았다.

> **[2026-09-05 정정] 위 "동점 처리" 진단은 틀렸다 — `eval-ir`은 이 코퍼스에서 결정적이다.**
>
> 오케스트레이터가 검증한 결과 세 가지가 전부 결정적이었다:
> - **HNSW 스캔**: 동점이 최대인 질의(완전히 같은 벡터 26행)로 **별도 커넥션 5회** 조회 →
>   반환 행과 순서가 완전히 동일.
> - **질의 임베딩**: **별도 프로세스 3회** → 1536차원 바이트 단위 동일
>   (같은 프로세스 안의 반복은 `embedding/cache.py` LRU에 맞아서 검증이 안 된다 — 위 본문의
>   "확인했다"는 그 함정에 걸린 것일 수 있다).
> - **`eval-ir` 전체**: 같은 코드·같은 코퍼스로 **4회 연속**(concurrency 4 ×2, 1 ×2) →
>   n=76 지표가 전부 동일하고 **질의 단위로도 0/76 차이**. 다른 두 작업자도 독립적으로
>   같은 결과(0/70, delta 0.0000)를 얻었다.
>
> **진짜 원인은 코퍼스 변조였다.** 같은 시각 다른 작업자가 `db backfill-slack-names`로
> 슬랙 문서 197건의 `documents.title`을 다시 쓰고 있었고(`#C0BSW8Y2AD9 …` →
> `#proj_2026-038-0_nh농협은행_mvp …`), 제목은 pg_trgm 입력이라 랭킹이 실제로 바뀐다
> (v0 ndcg@10 0.8516 → 0.8490). 위 표에서 "동일 코드"라던 두 run은 그 백필을 사이에 두고 있었다.
>
> **이게 진짜 함정이다: 제목 백필은 행 수도 `updated_at`도 `content_hash`도 바꾸지 않는다.**
> 그래서 `index_snapshot`·`check_staleness`·시각 대조가 **전부** 이 변조를 놓친다. 실제로
> 오케스트레이터도 `updated_at` 최댓값을 보고 "세션 이전에 이미 고쳐져 있었다"고 오진했다.
>
> **`search_by_embedding`에 tiebreak를 붙이지 마라.** 붙이면 플래너가 HNSW 인덱스를 버리고
> Seq Scan + Sort로 떨어진다(EXPLAIN으로 확인). 지금 코퍼스에선 안 느리지만 규모가 커지면
> 근사 ANN이 전수 정렬이 된다. 결정성은 이미 있으므로 얻는 것도 없다.
>
> 위 "측정 규칙에 주는 함의" 중 **살아남는 것은 두 번째 항목뿐이다** — 같은 코드로 baseline을
> 두 번 돌려 churn 바닥을 재는 습관. 바닥이 0이 아니면 그건 노이즈가 아니라 **누군가 DB를
> 쓰고 있다는 신호**다.
---

## `eval-ir`은 결정적이지 않다 - tie churn 바닥을 먼저 재고 읽어라

**증상.** 코드가 완전히 같은 baseline을 두 번 돌렸는데 76질의 중 **14건의 랭킹이 갈렸다**
(run `571b82c1d4104a12b14b3b78342c572d` vs `ff9c639310e446a9ad62c70d1de32a01`, 약 40분 간격).
`v0-sequence-001`은 코드 변경 없이 ndcg@10 0.177 → 0.000이 됐다. 그동안 코퍼스는
그대로였다(documents 358 / chunks 2856 / embeddings 2856, embedding_model 1종,
최신 임베딩 생성 시각도 그 앞).

**원인**(오케스트레이터 확인, 별도 브랜치에서 수정 중): `storage/postgres_store.py`의 벡터
검색이 `ORDER BY e.embedding <=> %s LIMIT %s`로 **결정적 tiebreak 없이** 자른다. 같은 파일의
어휘/제목 검색에는 `d.title, d.document_id` 같은 tiebreak가 붙어 있는데 벡터 경로만 빠졌다.
그리고 이 코퍼스에는 동점이 실제로 많다 - `knowledge_object_embeddings` 2,856행 중
**508행(17.8%)이 벡터가 완전히 같은 중복 그룹 134개**다(xlsx 보일러플레이트 청크: "Spacing O"
26회, "Page #" 24회, "[Legend]" 21회 …). 이 행들은 document_id가 서로 달라서, LIMIT 경계에서
어느 쪽이 살아남느냐에 따라 문서 랭킹이 run마다 바뀐다.

**그래서 측정 절차가 바뀐다.** "baseline 한 번, treatment 한 번" 페어드 비교로는 부족하다.
`--compare`의 평균 delta와 CI는 코드 효과와 tie churn을 구분하지 못한다.

1. **churn 바닥을 같은 창에서 먼저 재라.** baseline → treatment → baseline 순으로 **연달아**
   돌리고, 두 baseline끼리 비교한다. 그 둘이 갈린 질의 수가 그 창의 churn 바닥이다.
2. **변경 효과는 baseline 두 번 사이에 낀 treatment로 읽는다.** B2→T2와 T2→B3의 delta가
   부호만 반대로 **대칭**이면 그 차이는 코드 때문이지 churn이 아니다.
3. **집계 delta만 보지 마라.** 소수 질의를 결정론적으로 바꾸는 변경은 "어느 질의가 왜 바뀌었는지"를
   `eval_run_query.steps`에서 개별로 확인하는 쪽이 훨씬 신뢰할 수 있다 - 변경이 **구조적으로
   건드릴 수 있는 질의 집합**(예: 게이트에 걸리는 질의)과 **실제로 바뀐 질의 집합**이 일치하면
   인과가 확정된다.

**실측 예 (`_Run.link_cohesion` 채택 판단).** 위 절차로 연달아 3회:

| run | label | ndcg@10 |
|---|---|---|
| `42555f188f264047849d9ecd6e46d757` | CHURN B2 (baseline) | 0.8490 |
| `ff823130af35412ab1d2dae41042ee8c` | TREATMENT T2 (link-cohesion gated) | 0.8514 |
| `3bd86a5fb09d490093deec36708c5849` | CHURN B3 (baseline) | 0.8490 |

- **churn 바닥(B2 vs B3): 랭킹이 갈린 질의 0건.** 같은 창 안에서는 완전히 결정적이었다.
  (40분 간격에서 14건이 갈린 것과 대비된다 - churn은 시간이 아니라 그동안 공유 DB에 무슨 일이
  있었나에 달려 있다. 다른 작업자의 pytest DDL/재색인이 유력하다.)
- **효과(B2 vs T2, T2 vs B3): 각각 정확히 2건**, mean delta `+0.0024` / `-0.0024`로 대칭.
- 바뀐 2건은 `v0-sequence-001`(0.000 → 0.177)과 `v0-sequence-003`(1.000 → 0.990).
- **인과 확정:** 게이트(`_SEQUENCE_INTENT_RE`)에 걸린 질의는 4건(`sequence-001/002/003`,
  `long_range_context-002`)이고 그중 채널 클러스터가 실제로 있는 것은 2건이다
  (나머지 2건은 `steps`에 `reason: no_cluster`). **바뀔 수 있는 집합과 바뀐 집합이 정확히
  일치한다.**

따라서 "평균 +0.0024, 95% CI [-0.0004, +0.0076], 유의하지 않음"은 맞지만 읽는 법이 다르다:
이 변경의 값어치는 평균이 아니라 **"2건을 결정론적으로 바꾸고 74건은 손도 대지 않는다,
그중 손실은 1건이고 크기가 -0.010"** 이라는 데 있다. ±0.056 노이즈 바닥은 **랭킹 전체를 흔드는
노브**(하한, pool 크기, coverage 비율)를 판정할 때의 이야기고, 건드리는 질의 집합이 구조적으로
정해지는 변경에는 그 바닥이 아니라 위의 인과 확인을 적용해야 한다.

### 부수적으로 드러난 것: `--compare`의 경고를 믿어라

`--compare`는 두 run의 `config_hash`가 같으면 "delta는 config 변화가 아니라 run-to-run
노이즈를 잰다"고 경고한다. 랭킹 노브가 `config_hash`에 안 담기니 코드 변경 A/B에서는 이 경고가
늘 뜨고 그래서 무시하기 쉬운데, **tie churn이 실재하므로 그 경고는 문자 그대로 참이다.**
설정값을 `--label`에 박는 관례에 더해, 위의 B→T→B 절차를 같이 써야 한다.
## 슬랙 제목을 채널 ID에서 채널명으로 — 그리고 본문 발췌를 끝내 안 넣은 이유

### 출발점

개발 DB의 슬랙 문서 **197건 전부** 제목이 `#C0BGQEQG1L6 2026-09-01`이었다(채널 ID + 날짜).
v0 골든셋의 정답 문서 90건 중 **48건이 슬랙**이라, 제목 기반 조회(`document_search`)와
anchor 판정이 정답 절반 이상에 닿지 못한다는 진단으로 시작했다.

### 측정 1 — 제목 검색 경로만 떼어서 (오프라인)

`search_documents_by_title`의 3-tier(전체 substring / 토큰 전부 substring /
`word_similarity >= 0.3`)를 그대로 재현해, 358문서 전체를 후보로 두고 슬랙 정답을 가진
25개 질의를 돌렸다. 지표는 **슬랙 정답 문서 66건의 recall@10**.

| 제목 형태 | 슬랙 정답 recall@10 | 무관 질의 51건 top10에 낀 슬랙 문서 | 무관 질의 정답 recall@10 |
|---|---|---|---|
| `#<채널ID> <날짜>` (기존) | **0 / 66** | 0건 | 34/96 |
| `#<채널명> <날짜>` | **0 / 66** | — | — |
| `+ 첫 메시지 60자` | 34 / 66 | — | — |
| `+ 첫 메시지 120자` | **42 / 66** | 46건 | 33/96 |
| `+ 첫 메시지 200자` | 42 / 66 | — | — |

읽을 것 두 가지.

1. **채널명만으로는 아무것도 안 낫는다(0 → 0).** 사람은 슬랙을 찾을 때 채널 이름을 대지
   않는다. v0의 실제 질의는 "caddy-drift 아웃테이지 알림", "OCR 제품 교육 참석 여부를 묻는
   스레드", "명함 제작 논의 스레드"처럼 **첫 메시지 내용**을 댄다. 채널명은 후보 목록의
   가독성을 위한 것이지 검색 신호가 아니다.
2. **발췌는 120자에서 포화한다.** 200자로 늘려도 recall이 그대로인데 무관 질의 노이즈만
   늘어난다.

### 측정 2 — 파이프라인 전체 (eval-ir v0, `--no-llm`, 페어드)

같은 worktree에서 코퍼스만 바꿔 가며 연달아 돌렸다. 코드는 세 런 모두 동일(`da64fac+dirty`).

| 제목 형태 | ndcg@10 | run_id | vs 기준 |
|---|---|---|---|
| `#<채널ID> <날짜>` (기준) | 0.8516 | `da5cf39a0cc64bcf9576f020c6f00e26` | — |
| `#<채널명> <날짜>` | 0.8490 | `b77b9e65eda1400184bd48bd6943ae11` | −0.0025, CI [−0.0076, +0.0000], **유의하지 않음**, 0승 1패 69무 |
| `#<채널명> <날짜> <발췌 120자>` | 0.8498 | `62966bd1af93469eac681c02d6d3ffbd` | −0.0018, CI [−0.0293, +0.0233], **유의하지 않음**, 3승 5패 62무 |

**이 작업의 가장 중요한 결과가 이것이다: 진단의 전제가 절반만 맞았다.** 제목 경로가 슬랙
정답에 못 닿았던 것은 사실이지만(0/66), **시스템 전체로는 이미 벡터/청크 어휘 경로가 그
문서들에 닿고 있었다.** 기준선에서 슬랙이 몰린 유형은 이미 높았다 — conversation_thread
1.000, aggregation 0.888, sequence 0.786. 제목 경로는 그 문서들에 대해 **중복 경로**였다.
"검색 튜닝이 정답 절반을 포기한 채로 이뤄졌다"는 표현은 제목 경로에 대해서만 참이다.

### 발췌를 켰을 때 무엇이 움직였나 (3승 5패)

**얻은 것**

- `v0-exact_match-001`(`F0BUMH30V40`) 0.631 → **1.000**
- `v0-exact_match-002`(`git.sparklingsoda.ai:8443/nhbank/apps/card-reco.git`) 0.631 → **1.000**
- `v0-sequence-001`(risk-agent 아웃테이지 감지→복구 순서) 0.177 → **0.501**
  (채널명만 넣었을 때는 오히려 0.177 → 0.000이었다)

식별자·URL이 첫 메시지에 있는 스레드가 제목 substring 1.0으로 잡히면서 anchor가 되는 경우다.

**잃은 것 — 전부 같은 하나의 결함에서 나온다**

`search/retrieve.py::_metadata_filter_where`가 문서 종류 제약을 `d.title ILIKE '%가이드%'`로
건다. 이 술어는 **제목이 이름이라고 가정한다.** 본문 발췌가 제목에 들어오는 순간 가정이
깨지고, `_is_source_hint_only_filter`가 그 필터를 "믿을 수 있는 필터"로 분류해 교집합을
잡으면서 나머지 후보를 통째로 날려 버린다.

- `v0-whole_document_summary-003`("ProjectIntel Agent Guide(AGENTS.md) 전체 내용을 요약해줘")
  **0.631 → 0.000**. `title ILIKE '%매뉴얼%'`에, 첫 줄에 "[done] 운영 매뉴얼 초안"이 있는
  주간 다이제스트 스레드가 걸렸다. 결과가 그 슬랙 문서 1건으로 줄고 `whole_document`가
  그걸 읽었다.
- `v0-negative-001`("Kubernetes 클러스터 구성 가이드") **none_correct 1.0 → 0.0**.
  `%가이드%`에 슬랙 스레드 2건이 걸려, 정답이 "없음"인 질의가 결과를 냈다.
- `v0-multihop-002` 1.000 → 0.613. 다른 결함이다 — 기준선에서는 `document_search#semantic`이
  0히트라 term 단위 fallback이 발동해 gmail 정답까지 물어왔는데, 발췌 덕에 슬랙 1건이
  잡히자 **fallback 게이트가 닫혀** 그 1건이 결과 전부가 됐다.

나머지 2패(`v0-factoid-007` −0.131, `v0-sequence-002` −0.021, `v0-set_query-002` −0.019)는
순위 뒤섞임 수준이다.

### 결정

**`#<채널명> <YYYY-MM-DD>`로 간다. 발췌는 코드에 남기되 끈다**
(`storage/document_title.py::_SLACK_EXCERPT_CHARS = 0`).

세 형태가 통계적으로 구분되지 않는데(전부 ±0.056 노이즈 바닥 한참 아래), 발췌 쪽만 하드한
회귀(negative 질의가 답을 내는 것, whole_document가 엉뚱한 문서를 읽는 것)를 동반하기
때문이다. 그 회귀는 발췌 자체의 문제가 아니라 위의 `title_types` 결함이 드러난 것이므로,
**켜는 조건이 명확하다**: `_metadata_filter_where`의 문서 종류 술어가 `documents.title`을
종류 라벨로 쓰지 않게 고쳐지면 `_SLACK_EXCERPT_CHARS = 120`으로 되돌리고
`knowledge-forge db backfill-slack-names`만 다시 돌리면 된다. **재적재는 필요 없다** —
발췌의 원재료(`sources.metadata.first_message`)를 이미 저장해 뒀다.

채널명 자체는 검색 지표를 못 올렸지만 남긴다. `search/planner.py`가 후보의 `title`을 LLM에게
보여 주는 텍스트로 쓰는데(`hit.get("summary") or hit.get("snippet") or hit.get("title")`),
`#C0BGQEQG1L6`은 LLM에게도 아무 의미가 없다. 이 코퍼스의 채널명은
`proj_2026-038-a_nh농협은행_마케팅허브-메시지-ai-agent-구축`처럼 프로젝트 코드까지 담고 있다.
이 이득은 `--no-llm` 런이 측정하지 않는 축이다.

### 이 작업이 새로 드러낸 결함 3건

1. **`_metadata_filter_where`의 `title_types` 술어**가 `documents.title`을 문서 종류
   라벨처럼 쓴다(`title ILIKE '%가이드%'`). 제목에 본문이 조금이라도 섞이면 오탐이 나고,
   `_is_source_hint_only_filter`가 그 필터를 신뢰해 교집합을 잡으므로 **오탐 1건이 결과
   전체를 대체한다**. negative 질의까지 깨진다.
2. **term 단위 fallback 게이트가 "0히트"에만 반응한다.** 약한 1히트가 잡히면 fallback이
   닫혀서, 넓게 물어와야 답이 완성되는 multihop 질의가 그 1건으로 잘린다(`v0-multihop-002`).
3. **`_metadata_filter_where`가 `participant_names`를 보지 않는다.** 이제 이름을 채워 놨는데도
   "누가 보낸" 류 질의는 여전히 슬랙에 못 닿는다. `_person()`에 OR 하나 얹으면 된다.

### 재현

```
# 백필 (멱등, sources.metadata + documents.title만 씀)
uv run --extra dev --extra slack knowledge-forge db backfill-slack-names --dry-run
uv run --extra dev --extra slack knowledge-forge db backfill-slack-names

# 페어드 측정 (같은 worktree에서 코퍼스만 바꿔 연달아)
uv run ... knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --label <설정값 박은 라벨>
uv run ... knowledge-forge eval-ir --compare <32자 run_id A> <32자 run_id B> --metric ndcg@10
```

### 왜 이득이 안 났나 — `eval_run_query.steps`로 확인한 답

"골든셋 정답 90개 중 48개가 슬랙인데 제목을 고쳤는데 왜 지표가 안 오르나"가 핵심 질문이다.
`document_search`(제목 조회)가 각 질의에서 **실제로 슬랙 문서를 후보로 물어왔는지**를
`eval_run_query.steps`의 `detail.candidates`에서 세었다. 슬랙 정답을 가진 25개 질의 기준:

| 제목 형태 | 제목 조회가 물어온 슬랙 문서 | 제목 히트가 하나라도 생긴 질의 |
|---|---|---|
| `#<채널ID> <날짜>` | **0건** | **0 / 25** |
| `#<채널명> <날짜>` | 7건 | **2 / 25** |
| `+ 첫 메시지 120자` | 27건 | 7 / 25 |

76개 질의 전체로 보면, 제목 조회가 낸 후보 중 슬랙은 각각 0/67, 10/70, 37/89건이다.

**첫 줄이 원래 진단이 옳았음을 확인해 준다** — 채널 ID 제목일 때 제목 경로는 슬랙 문서를
**단 한 건도** 물어오지 못했다. 완전한 구조적 실명이었다.

**그런데 채널명으로 고쳐도 25개 중 23개는 여전히 0건이다.** 사람이 슬랙을 찾을 때 채널
이름을 대지 않기 때문이다. v0의 실제 질의는 "caddy-drift 아웃테이지 알림", "OCR 제품 교육
참석 여부를 묻는 스레드", "3차 인터뷰 체크리스트 파일 전달 메시지"처럼 **첫 메시지 내용**을
묻는다. 채널명은 질의 표면에 등장하지 않는다.

그리고 히트가 생긴 2건마저 지표를 못 올렸다:

| 질의 | ID제목 | 채널명 | +발췌 |
|---|---|---|---|
| `v0-conversation_thread-002` | 1.000 | 1.000 | 1.000 |
| `v0-sequence-001` | 0.177 | **0.000** | 0.501 |

- `conversation_thread-002`는 **이미 1.000**이었다. 벡터/청크 어휘 경로가 이미 정답을 1위로
  올려놨으니 제목 표가 하나 더 붙어도 올릴 자리가 없다.
- `sequence-001`은 **오히려 떨어졌다**. 채널 ID `C0BGQEQG1L6`이 제목에서 사라지면서, 질의에
  섞여 있던 식별자성 문자열과의 약한 trigram 매칭을 잃었다.

이 둘이 `−0.0025`의 전부다. **"제목을 고쳤으니 좋아졌을 것"은 틀렸다 — 실측은 반대다.**
제목 경로의 실명은 진짜였지만, 그 경로는 이 코퍼스에서 벡터/어휘 경로와 **중복**이었다.
제목 조회가 유일한 경로인 질의(짧은 navigational 질의)가 슬랙에는 사실상 없다.

### 부록: `eval-ir`은 이 코퍼스에서 결정적이다 (실측)

위 페어드 비교를 해석하려면 run-to-run 변동 폭(churn)을 알아야 한다. **코드·코퍼스·설정이
완전히 같은 두 런**을 연달아 돌린 결과:

```
A=dfd28768611245c0b0e565fba2ba1638  B=a565122b2f0e4ad888868122453788fd
mean A=0.8490 B=0.8490 delta=+0.0000 95% CI [+0.0000, +0.0000]
win/loss/tie = 0/0/70
```

**질의 하나도 안 갈렸다.** 그러므로 위 표의 승/패는 전부 코퍼스 변경이 실제로 일으킨
결정론적 차이지 노이즈가 아니다. `±0.056`은 골든셋 76개라는 **표본 크기**에서 오는 불확실성
(같은 시스템을 다른 76개 질의로 재면 얼마나 흔들리는가)이지 재실행 변동이 아니다. 둘을
섞으면 안 된다 — 재실행 변동이 0이라는 것은 "질의 1개가 갈린 것"이 진짜 변화라는 뜻이고,
`±0.056`은 그 변화가 **모집단에서도** 개선인지를 말해 주지 않는다는 뜻이다.

(세션 중 "벡터 동점 때문에 8질의가 churn한다"는 가설이 돌았지만, 이 측정과 오케스트레이터의
독립 검증 양쪽에서 기각됐다. 실제로 다른 작업자가 관측한 "같은 코드 두 런이 갈렸다"의 원인은
**이 작업의 제목 백필이 그 사람 측정 중간에 코퍼스를 바꾼 것**이었다. 교훈: corpus를 쓰는
작업은 `--label`에 그 사실을 박고, 쓰기 전후를 자기 worktree에서 연달아 재라.)

### 부록: 백필 전 상태 스냅샷과 되돌리기

`documents_title_backup_slack` 테이블(및 worktree의 `documents_title_backup_slack.csv`)에
백필 직전 197건의 제목이 그대로 있다. 되돌리려면:

```sql
UPDATE documents d SET title = b.title_before
FROM documents_title_backup_slack b WHERE d.document_id = b.document_id;
```

백업 테이블이 없어도 **역산이 가능하다.** 백필은 원시 `sources.metadata.channel`을 지우지
않으므로 예전 제목은 `'#' || (s.metadata->>'channel') || ' ' || to_char(content_at, 'YYYY-MM-DD')`로
그대로 재현된다 — 실측으로 197/197건이 백업본과 정확히 일치한다.

```sql
-- 백업 테이블 없이 되돌리기
UPDATE documents d SET title = '#' || (s.metadata->>'channel') || ' ' ||
       to_char(COALESCE(s.modified_at, s.collected_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD')
FROM sources s WHERE s.source_id = d.source_id AND s.source_type = 'slack';
```

주의: `db backfill-slack-names`도 `backfill_document_titles`도 **`documents.updated_at`을
건드리지 않는다**(제목 채우기는 내용 개정이 아니라는 설계 판단). 그래서 `updated_at`을 보고
"제목이 언제 바뀌었나"를 추론하면 **틀린다** — 실제로 이 세션에서 한 번 그 착오가 있었다.
제목 변경 이력을 알고 싶으면 이 백업 테이블을 봐라.
