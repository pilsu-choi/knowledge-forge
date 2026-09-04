# Handoff — 검색 전략 보강 + 골든셋 IR 평가 (2026-09-04)

새 세션에서 이 작업을 이어받기 위한 인수인계 문서. 설계 근거는 코드에 남지 않으므로
`query-types.md`(질의 유형 20종), `search-architecture.md`(①~⑦ 단계별 구현 상태),
`search-evaluation.md`((A)~(D) 평가 조각)를 함께 본다.

## 1. 지금 어디까지 됐나

작업 브랜치는 **`feature/search-strategy`**, worktree는
`/home/pilsu/projects/Knowledge-Forge/.claude/worktrees/search-strategy` (main `46cb5af`에서 분기).
**`main`에 커밋하지 않았고 push도 하지 않았다.**

| 커밋 | 내용 |
|---|---|
| `d6163b6` | `search/constraints.py` — 질의 제약 추출(규칙 + LLM 보강) + `suggest_strategies` |
| `8f24d5b` | `documents.title` 파생 + pg_trgm `document_search`/`lexical_search` + `db backfill-titles` |
| `2a11e2e` | `search/document.py` — `read_document` 도구, `GET /documents/{id}/chunks` |
| `e2c940b` | 골든셋 스키마 `evaluation/golden.py` + `evaluation/ir_metrics.py` + `tests/eval/golden/v0.json` |
| `3e8ad1a` | 인제스트 버전 메타데이터 `ingest_version.py` + `document_links` 테이블 + `db backfill-links` |
| `b27dd09` | `knowledge-forge eval-ir` CLI + `eval_run`/`eval_run_query` 테이블 + paired A/B 비교 |
| `6a975e1` | (문서) search-evaluation.md 상태 갱신 |
| `5a0a20e` | `search/retrieve.py` — 검색 전략 실행기 + 에이전트 `search` 도구 |
| `2c4b1da` | `RetrieveFn` 프로토콜 `steps`를 읽기 전용 속성으로(mypy) |
| (HEAD) | `execute_readonly_query` 직렬화 + v0 baseline 실측 기록 |

게이트 상태: `uv run ruff check src tests` 통과, `uv run mypy src` 통과,
`KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests -q --deselect tests/api/test_api.py`
→ **744 passed**. (`tests/api/test_api.py`의 ingest/jobs 테스트는 Redis(:6379)가 없어서 실패하는
기존 환경 문제라 deselect한다.)

계획했던 1·2단계는 **전부 머지됐고 v0 baseline도 실측했다.** 아래 2절은 다음 세션이 할 일이다.

## 2. 다음에 할 일

### (a) 임베딩 채우고 baseline 다시 — **가장 큰 미지수**

지금 baseline은 임베딩 0행이라 `vector` 전략이 통째로 빠진 숫자다. `.env`에
`OPENAI_API_KEY`를 넣고 `KF_EMBEDDING_SOURCE=chunk`로 임베딩을 생성한 뒤 다시 돌려
`--compare`로 벡터가 실제로 얼마나 보태는지 본다.

```bash
knowledge-forge eval-ir --golden tests/eval/golden/v0.json --label with-vector --json out/v0-vector.json
knowledge-forge eval-ir --compare fc96003a531547a0b04267a2ca7b2844 <new_run_id> --metric ndcg@10
```

`--no-llm` 없이 돌리는 것도 별도 축이다. **multihop이 0.000인 건 검색 품질이 아니라
`--no-llm`이면 planner가 안 도는 탓**이므로, LLM을 켠 run이 먼저 필요하다.

### (b) `knowledge-forge eval-gen` — 골든셋 v1

v0는 손으로 쓴 미검수 스모크셋이다(5절). 절대값 기준이 되려면 `chunks.text`에서 층화 샘플링 →
LLM 질의 생성 → 자동 필터 → 사람 검수(Tier A)를 거친 **v1**이 필요하다. v0는 그대로 둔다.
설계는 `search-evaluation.md` (C) "다음 단계"에 있다.

### (c) 낮은 유형 파고들기

`conversation_thread` 0.250 / `sequence` 0.343 / `factoid` 0.497. 앞의 둘은 `document_links`의
`thread_sequence`/`same_channel_sequence`를 `retrieve()`가 아직 랭킹에 쓰지 않는 게 원인일
가능성이 크다(링크는 백필돼 있다). 질의별 `steps`가 `eval_run_query`에 통째로 남아 있으니
검색을 다시 돌리지 않고 어느 전략이 뭘 반환했는지 볼 수 있다.

### (d) 정리

- `feature/search-strategy`를 `main`에 머지할지 결정 (**지금까지 push 안 함**)
- 서브에이전트 worktree 정리: `git worktree remove .claude/worktrees/agent-*`
  (7개: `a226ccc74a9477241`, `a4644f1c29a501ab7`, `a5d65861b553e5bf9`, `a68b5d9fb87fdefef`,
  `a89a74559b56d94b1`, `ac412f1706e93dea8`, `af32819753682d15b`)

## 3. 이번 작업의 배경 결정 (코드에 안 남는 것)

- 사용자가 제시한 **질의 유형 20종**(Navigational / Whole-document Summary / Temporal /
  Metadata Filter / Aggregation / Set Query / Negative / Latest / Version / Parent-Child /
  Multi-hop / Ambiguous …)을 골든셋에 **multi-label**(`types[]` + `primary_type`)로 넣기로 했다.
  정답 모양은 `expected: {document_ids, exhaustive, count, none, chunk_ids, knowledge_object_ids}`.
- **검색 보강을 골든셋 실행보다 먼저** 했다. 이유: `eval-ir`는 에이전트 루프 없이 검색을
  호출하는데, 메타데이터/집계/시간 질의는 그전까지 "에이전트가 `sql_search`를 고른다"는
  방식으로만 풀렸다. 결정론적 진입점 `retrieve(query)`가 없으면 채점 자체가 불가능하다.
  → 그래서 `retrieve.py`가 이번 작업의 핵심이고, (a)가 아직 안 끝났다는 게 중요하다.
- Version / Parent-Child는 "인제스트 단계에서 메타데이터를 더 뽑아야 한다"는 이유로 한 번
  범위에서 빠졌다가, 서브에이전트 병렬 작업으로 **이번 범위에 다시 포함**됐다(`3e8ad1a`).

## 4. 개발 DB의 실측 한계 (평가 해석에 필요)

`postgresql://kf:kf@localhost:15432/knowledge_forge` (docker compose, superuser `kf`):

- documents 263 / chunks 936 / knowledge_objects 5107 / sources 502 (gdrive 246, slack 197, gmail 59)
- **gdrive 246개 소스 중 Document가 있는 건 7개뿐** — 파서가 붙은 것만 문서화됐다.
- **`knowledge_object_embeddings` 0행.** 임베딩 provider는 `openai`(`text-embedding-3-small`)
  뿐이고 `.env`에 `OPENAI_API_KEY`가 없다. → **`vector` 전략은 지금 평가 불가**이고,
  `retrieve()`는 임베딩이 없으면 키워드 fallback으로 돈다. 벡터 평가를 하려면 키를 넣고
  `KF_EMBEDDING_SOURCE=chunk`로 임베딩을 생성해야 한다.
- `documents.metadata.title`은 전부 NULL이었다 → 제목은 `sources.metadata`(gdrive `name`,
  gmail `subject`, slack `channel`)에서 파생한다. `db backfill-titles`로 263/263 백필 완료.
- `document_links` 백필 결과: family 66, `same_channel_sequence` 145, `thread_sequence` 58,
  `previous_version` 0, `thread_parent` 0.
- `relations`에 문서 간 edge 0 / `chunks.parent_chunk_id` 전부 NULL — multihop·parent_child
  질의는 현재 데이터로는 근거가 약하다.
- 작업 중 `idx_sources_project_id` btree 인덱스가 깨져 있어 `REINDEX`로 고쳤다. 비슷한
  증상(정상 쿼리가 빈 결과)이 나오면 인덱스를 의심할 것.

### v0 baseline 실측 (`run_id=fc96003a531547a0b04267a2ca7b2844`)

```
n=76 hit@1=0.657 recall@5=0.697 mrr=0.706 ndcg@10=0.709
     exhaustive_recall=0.778 count_match=0.500 none_correct=1.000
```

잘 됨: long_range_context/parent_child 1.000, exact_match 0.952, version 0.947, navigational 0.830.
안 됨: multihop 0.000(=`--no-llm`이라 planner 미실행), conversation_thread 0.250, sequence 0.343,
factoid 0.497(벡터 없이 패러프레이즈를 못 푼다). 유형별 표는 `search-evaluation.md`.

**동시 실행 함정**: `PostgresStore`는 커넥션 하나를 공유하는데 `execute_readonly_query`만
명시적 트랜잭션을 연다. `--concurrency`가 1보다 크면 겹쳐서 `OutOfOrderTransactionNesting`이
났다(첫 baseline에서 76개 중 20개 실패). 그 메서드에 `asyncio.Lock`을 걸어 고쳤다. 앞으로
`PostgresStore`에 트랜잭션을 여는 메서드를 추가하면 **같은 락을 써야 한다.**

## 5. 골든셋 v0의 성격 — 그대로 믿으면 안 됨

`tests/eval/golden/v0.json`: 76개, `project_id: null`, `as_of: 2026-09-04T00:00:00Z`,
유형 22종 커버(factoid 10, navigational 4, metadata_filter 4, specific_document_qa 4, 나머지 3개씩).

- **손으로 작성했고 사람 검수를 거치지 않았다.** 절대값 기준이 아니라 배관(스키마·러너·지표)이
  도는지 확인하는 스모크용이다.
- 개발 DB의 실제 document_id를 참조하므로 **DB가 재인제스트되면 stale**해진다
  (`source_hash` + `check_staleness`가 경고를 낸다. 경고만 하고 집계에서 빼지는 않는다).
- 절대값 기준은 `eval-gen`으로 만들 **v1**(합성 + 사람 100% 검수, Tier A)이다. v0는 그대로 둔다.

## 6. 작업 규칙 (`Agents.md`)

- 모든 작업 내용과 고민은 **wiki에 기록**한다.
- 서버에 영향 없게 **새 브랜치 + worktree**를 만들어 작업하고 **한글로 커밋**한다.
- 병렬 가능한 작업은 **서브에이전트로 병렬 수행**한다. 이번 작업은 1단계 5개 + 2단계 2개를
  각각 격리 worktree(`.claude/worktrees/agent-<id>`, 브랜치 `worktree-agent-<id>`)에서 돌리고
  통합 브랜치에 `--no-ff`로 머지했다. 서브에이전트에게는 **인터페이스 계약을 먼저 고정해서**
  주면 충돌이 거의 안 난다(2단계에서 `RetrievalResult` 계약을 두 에이전트에 동일하게 줬다).
