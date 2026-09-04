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

게이트 상태(`6a975e1` 기준): `uv run ruff check src tests` 통과, `uv run mypy src` 통과,
`KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge uv run pytest tests -q --deselect tests/api/test_api.py`
→ **718 passed**. (`tests/api/test_api.py`의 ingest/jobs 테스트는 Redis(:6379)가 없어서 실패하는
기존 환경 문제라 deselect한다.)

## 2. 아직 안 끝난 것 — 가장 먼저 할 일

### (a) `search/retrieve.py` 브랜치 머지 ← **여기서부터 시작**

`worktree-agent-a89a74559b56d94b1` 브랜치 / worktree
`.claude/worktrees/agent-a89a74559b56d94b1`에 **작업이 전부 staged 상태이지만 커밋되지 않았다.**
(서브에이전트가 커밋 직전에 멈춤. worktree가 `locked`이면 `git worktree unlock <path>`.)

staged 파일:

```
src/knowledge_forge/search/retrieve.py   (신규, 717줄)
src/knowledge_forge/agent/tools.py       (search 도구 추가)
src/knowledge_forge/agent/prompts.py
tests/search/test_retrieve.py            (신규, 640줄)
tests/agent/test_tools.py
wiki/search-architecture.md              (① Query Understanding 절 갱신)
```

이어받는 절차:

```bash
cd /home/pilsu/projects/Knowledge-Forge/.claude/worktrees/agent-a89a74559b56d94b1
uv run ruff check src tests && uv run mypy src
KF_TEST_DATABASE_URL=postgresql://kf:kf@localhost:15432/knowledge_forge \
  uv run pytest tests/search tests/agent tests/test_cli.py -q
git commit -m "feat: 검색 계획 실행기 search/retrieve.py + search 에이전트 도구"   # 한글 메시지 + 트레일러
cd ../search-strategy && git merge --no-ff --no-edit worktree-agent-a89a74559b56d94b1
```

충돌 예상 지점: `agent/tools.py`, `tests/agent/test_tools.py`, `wiki/search-architecture.md`.
지금까지의 충돌은 전부 "both added" 형태였고 **양쪽을 다 남기는 방식**으로 해결했다.
도구 등록 순서는 `file_search, vector_search, sql_search, document_search, lexical_search,
read_document, search, plan_search`.

### (b) 골든셋 v0 baseline 실측

머지 후 개발 DB에 대해 한 번 돌리고 `run_id`를 `wiki/search-evaluation.md`에 기록한다.

```bash
knowledge-forge eval-ir --golden tests/eval/golden/v0.json --no-llm --label baseline \
  --json out/v0-baseline.json
```

이후 전략을 바꾸면 `eval-ir --compare <baseline_run_id> <new_run_id> --metric ndcg@10`.

### (c) 마무리

- `wiki/search-evaluation.md` "다음 단계"에 baseline `run_id` 기록
- 메모리 `search-strategy-wip.md` 삭제, `search-eval-golden-set-plan.md`를 "1~3번 완료, 다음은
  eval-gen"으로 갱신
- 다 끝나면 서브에이전트 worktree 정리: `git worktree remove .claude/worktrees/agent-*`

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
