# 검색 로직 코드 리뷰 (2026-09-14)

결정론 파이프라인(`search/*`)과 에이전트 경로(`agent/*`)를 `feature/agentic-eval` 워킹트리 기준으로 리뷰한 결과다.
**아직 아무것도 고치지 않았다.**

✔ 표시는 직접 재현했거나 코드로 확인한 항목이다. 나머지는 병렬 리뷰 두 건의 결과이며 따로 검증하지 않았다.

## 결론

- **결정론 파이프라인**: 구조는 잘 잡혀 있다. 다만 점수를 깎는 버그가 여럿 있다(병합 키, anchor 붕괴, 시간 파싱).
- **에이전트 경로**: 파이프라인에 LLM을 한 겹 씌운 수준이다. 약점은 도구 경계에 몰려 있다(`project_id` 덮어쓰기, 10건 절단, JSON 파싱).

## 결정론 파이프라인

### 흐름

1. `retrieve()`(`retrieve.py:1438`)가 `extract_constraints_rules`(`constraints.py:555`)를 부른다. LLM 보강은 `:799`에서 한다.
2. `suggest_strategies`(`constraints.py:877`)가 전략을 고른다.
3. arm이 차례로 돈다.
   - `document_lookup`(`:718`, anchor를 판정한다)
   - `lexical`(`:745`)
   - `metadata_filter`(`:762`)
   - `vector`(`:794`)
   - `keyword_arm`(`:875`)
   - `_term_fallback`(`:961`, coverage 게이트와 희귀 term 게이트)
4. 병합(`:1220`)은 `merge_and_rerank` RRF(`rerank.py:54`) → link_cohesion → 필터 교집합 → anchor 고정 순서다.
5. 후처리 단계는 latest, whole_document, limit 절단, count다.

### 잘된 점

- **결정론**: gather가 입력 순서를 보존하고, 모든 SQL에 tiebreak가 있으며, steps에 판단 근거가 남는다.
- **안전**: SQL 이스케이프, NFC 정규화, READ ONLY 트랜잭션과 강제 롤백, `hnsw.iterative_scan` 적용.
- **장애 격리**: 벡터 장애만 격리하고, 다른 arm의 버그는 숨기지 않는다.

### 결함 — 높음

1. **RRF가 문서 단위로 합산되지 않는다** (`rerank.py:42-51`)
   - 제목 검색과 SQL 필터 히트에는 ko_id, chunk_id, path가 없다. 그래서 키가 `id(hit)`가 된다.
   - 결과적으로 같은 문서라도 제목 arm의 표와 벡터 arm의 표가 합쳐지지 않는다.
   - term fallback도 청크 단위라서, term이 서로 다른 청크에 있으면 합산되지 않는다.
2. **어휘 arm의 anchor가 전수 질의를 한 문서로 붕괴시킨다** (`retrieve.py:917-919`, `:343`, `:958`)
   - 제목 substring 매치가 하나뿐이면 모든 arm이 그 문서로 좁혀지고, term fallback도 꺼진다.
   - 실측: `v1-entity_centric-001` "한화손해보험 관련 문서 전부"는 정답 7건 중 2건만 나왔다(ndcg 0.448). 같은 유형의 나머지 평균은 0.901이다.
3. ✔ **"작년 3분기"가 올해 3분기로 해석된다** (`temporal.py:255`, `constraints.py:519`)
   - bare 분기 분기가 먼저 매치되면 "작년" 보정이 실행되지 않는다. "Q1 2025"도 2026년 Q1이 된다.
   - `"작년 3분기에 김민수가 보낸 메일"`(now=2026-09-14) → `2026-07-01 ~ 2026-10-01`
4. ✔ **"지난 4월"이 최근 4개월로 해석된다** (`temporal.py:113`)
   - `_RECENT_RE`의 단위 `개?월`이 bare "월"도 받는다. 같은 이유로 "지난 12일"도 최근 12일이 된다.
   - `"지난 4월 회의록"` → `start=2026-05-01, label='최근 4개월'`

### 결함 — 중간

5. ✔ **마커와 필러가 낱말 안에서도 매치된다** (`_FILLER_RE` `constraints.py:390`, 마커 `:171`, `:140`)
   - `"체크리스트 문서"` → `wants_exhaustive=True`, `semantic_query='체크'`
   - "도메인지식"은 "지식"으로, "회의자료"는 "회의"로 잘린다.
   - "제외 파일"이 들어가면 negative로 판정된다.
6. ✔ **작성자 regex가 "님"을 이름에 포함한다** (`constraints.py:355`)
   - `"김철수님이 보낸 메일"` → `authors=['김철수님']`, 그리고 semantic_query가 빈 문자열이 된다.
7. **source_type 추측이 구조적 술어와 같이 오면 하드 필터가 된다** (`retrieve.py:466`)
   - 예: "지난주 드라이브 관련 논의"
8. **negative/exhaustive 전략에 실행기가 없다** (`retrieve.py:658`)
   - `wide`를 켤 뿐이라 부재 판정 로직이 없다.
   - "없는", "제외" 같은 흔한 어휘로 켜지면 top-k 절단이 사라진다.
9. **coverage를 term당 상위 20청크로만 계산한다** (`retrieve.py:977`, `:1004`)
   - 흔한 term이면 모든 term을 담은 문서도 임계값에서 탈락한다. 알려진 31건 절벽의 원인 후보다.
10. **날짜 경계가 UTC 기준이다** (`temporal.py:150`, `:191`)
    - sources 502건 중 15건이 KST 기준으로 다음 날이다.

### 결함 — 낮음

- "지난해"를 인식하지 못하고, "올해 3월"은 올해 전체로 해석된다.
- `_DOMAIN_RE`가 "socket.io"를 참여자 필터로 흡수한다.
- link_cohesion docstring과 코드가 어긋난다(`:1156` vs `:1161`).
- `scores`가 척도가 서로 다른 raw score의 최대값이다(`:321`).
- `filter_only` 분기는 도달할 수 없다.

### 설계 위험

- **골든셋 과적합**: 분기 대부분이 특정 질의 하나를 고치려고 생겼다. 실행 순서에 의존하는 상태도 여럿이다.
- **마커 사전**: 한국어 substring 매치라 어휘가 늘수록 5번 같은 오탐이 선형으로 는다. 형태소 분석기(`korean` extra)로 토큰 경계를 거는 것이 근본 해결이다.
- **흩어진 임계값**: 아래 값들이 서로 독립적으로 결과를 0건으로 만들 수 있다.
  - vector_min_score 0.45
  - coverage 2/3
  - 희귀 term 게이트
  - anchor 격차 0.2
- **성능**: 청크 어휘 검색의 `OR word_similarity() >= 0.5` 때문에 매번 순차 스캔이 돈다.

## 에이전트 경로

### 구조

- **루프** (`loop.py:186-205`): 압축 → `provider.send` → 도구 호출이 없으면 종료한다. 도구 호출이 있으면 `gather`로 동시에 실행한다.
  - `max_iterations=8`에 도달하면 도구 없이 답변을 한 번 강제한다.
- **도구 결과**: `_wrap_untrusted`로 감싼다. 도구 예외는 `"error: …"` 문자열로 바뀐다.
- **기본 도구 표면(minimal)**: `search`, `read_document`, `sql_search`, write_memory, write_plan.
- **압축** (`compaction.py:140`): 앞 1개와 뒤 8개 메시지를 보호하고, 도구 호출과 결과의 경계를 맞춘다.

### 잘된 점

- 반복 상한에 도달해도 실패로 끝내지 않고 답변을 받는다.
- 도구 하나가 실패해도 나머지 도구 호출은 취소되지 않는다.
- 도구 결과를 신뢰할 수 없는 데이터로 감싸고, limit과 max_chars를 서버에서 한 번 더 제한한다.
- Anthropic 프롬프트 캐시 중단점이 적절한 위치에 있다.

### 결함 — 높음

1. ✔ **`project_id`를 검증하지 않고, 모델이 넣은 값이 세션 값을 덮어쓴다** (`tools.py:492`, `args.get("project_id") or project_id`)
   - 스키마(`tools.py:243`)에 enum이나 유효 목록이 없고, 0건 응답에도 이유가 없다. 모델은 결과가 없는 줄 알고 포기한다.
   - gpt-4o-mini의 파이프라인 대비 손실 거의 전부와 Qwen3-30B 0.266의 원인이다.
   - 수정안: 유효한 id를 enum으로 넣거나, 없는 id가 오면 오류와 함께 유효 목록을 돌려준다.
2. ✔ **전수·개수 질의가 10건에서 잘린다** (`tools.py:517`, `document_ids[:limit]`)
   - `retrieve`는 exhaustive/count 질의에서 최대 200건(`EXHAUSTIVE_LIMIT`)을 돌려주지만, 도구가 기본 10건으로 자른다.
3. ✔ **도구 인자 JSON이 깨지면 실행 전체가 죽는다** (`providers/openai_compatible.py:110`)
   - 보호 없는 `json.loads`라서 Qwen에서 관측된 `JSONDecodeError`가 이 경로로 난다.
   - 수정안: 파싱 실패를 도구 결과 `"error: invalid JSON arguments"`로 돌려줘서 모델이 다시 시도하게 한다.

### 결함 — 낮음

- 필수 인자가 빠지면 `error: 'query'`처럼 뜻을 알 수 없는 메시지가 간다(`loop.py:105`).
- 프롬프트에 없는 도구 설명이 남아 있다: delegate_task 절, `time_range` 인자 지침(`prompts.py:68-74`, `:35`).
- full 표면의 plan_search 설명이 기본으로 꺼진 file_search를 가리킨다(`tools.py:666`).
- 압축 요약이 실패하면 가운데 도구 결과가 자리표시로 대체되어 인용 근거가 사라진다(`compaction.py:137`).

### 설계 위험

- **도구 표면이 "파이프라인 한 번 호출"로 유도한다.** 프롬프트가 search를 "질문 원문 그대로" 부르게 한다(`prompts.py:22-28`).
  - 실측으로 150회 중 136회가 search 1회로 끝났다.
- **보충 수단이 후보를 늘리지 못한다.** read_document는 이미 찾은 문서만 읽고, 질의를 바꿔 쓰거나 후속 검색 결과를 합칠 방법이 없다.
- **기대 성능의 상한은 "파이프라인 − 모델 실수"다.** 지어낸 `project_id` 회차 5회를 빼면 0.848로, 파이프라인 0.842와 동률이다.
  - 개선 방향은 두 가지다.
    - 0건이거나 결과가 적을 때 steps에 이유와 다음 질의 제안을 싣는다.
    - 후속 검색 결과를 순서대로 붙이지 말고 RRF로 합친다.

### 평가 코드 관련 (삭제 예정)

`evaluation/agent_runner.py`에서 발견한 문제다. 평가 서브시스템은 `chore/remove-evaluation`에서 삭제 중이므로, 재구현할 때 교훈으로만 쓴다.

- 병렬 도구 호출 시 랭킹이 호출 순서가 아니라 완료 순서를 따른다.
- 도구 안의 제약 추출 LLM 호출이 `retrieval_ms`와 `generation_ms`에 이중으로 잡힌다. 제약 추출에 에이전트 모델이 쓰이는 비대칭도 있다.
- `_hit_cap`이 8번째 반복에서 정상 답변을 해도 True가 된다.

## 추천 수정 순서

1. 에이전트 1~3번: `project_id` 검증, 결과 상한 올리기, 깨진 JSON 처리
2. 파이프라인 3, 4, 6번: 시간 파싱과 "님" 처리. 각각 regex 한 줄 수준이다.
3. 파이프라인 1, 2번: 문서 단위 병합, 전수 질의에서 anchor 끄기

골든셋이 평가 코드와 함께 삭제되는 중이라 지금은 1·2번의 효과를 잴 수 없다.
평가를 재구현한 뒤에 진행하거나, 기존 git 이력(`403b16e`)을 임시 기준으로 쓴다.

## 수정 내역 (fix/search-parsing)

2026-09-14, `fix/search-parsing` 브랜치(base main `a9db6f9`)에서 이 리뷰가 가리킨 명백한
파싱·도구 버그를 고쳤다. 커밋 `2eaf56e`(시간 파싱 + 마커), `2d69d49`(JSON 파싱 + 루프),
`3f1a522`(agent 도구). 랭킹 구조(결함1·2, 문서 단위 RRF·anchor 붕괴)는 이번 범위가 아니다
(계획서 S2).

### 고쳐진 항목

| 리뷰 항목 | 상태 | 비고 |
|---|---|---|
| ✔ 결함3 "작년 3분기"→올해 3분기 | 완전 수정 | `_supplemental_time_range`가 연도 문맥(작년/올해/지난해/재작년)을 먼저 잡고 그 안에서 분기/월을 해석(`temporal.py::resolve_within_year`) |
| ✔ 결함4 "지난 4월"→최근 4개월 | 완전 수정 | `_RECENT_RE` 월 단위를 `개월\|달`로 한정, "지난 &lt;월&gt;"은 별도 규칙(`_LAST_BARE_MONTH_RE`)으로 가장 최근에 지나간 그 달로 해석 |
| 결함5 마커/필러 낱말 중간 매치 | 완전 수정(7건 전부) | `_korean_eojeol_search`(왼쪽 어절 경계) + 앞쪽 일부로 잡히는 3건(전문가/도메인지식/대한민국)은 해당 마커에 부정 전방탐색 직접 부착 |
| ✔ 결함6 "님" 흡수 | 완전 수정 | `_strip_honorific` |
| 결함7 `_DOMAIN_RE`가 "socket.io" 흡수 | 완전 수정(트레이드오프 있음) | `_TLD_ALT`에서 "io" 제거 - 실제 io 메일 도메인이 코퍼스에 없다는 전제, 아래 "남겨둔 것" 참고 |
| 결함10 날짜 경계 UTC 기준 | 완전 수정 | `temporal.py::local_date_start`가 모든 경계를 Asia/Seoul(KST, DST 없음)로 계산 후 UTC 변환 |
| "Q1 2025"(분기가 연도 앞) | 완전 수정 | `_QUARTER_WITH_YEAR_RE`에 어순 반대 분기 추가 |
| "지난해" 미인식 | 완전 수정 | `_LAST_YEAR_RE`에 추가 |
| ✔ 에이전트 결함1 `project_id` 검증 없음 | 완전 수정(도구 4개) | search/vector_search/document_search/lexical_search 스키마에서 `project_id` 제거, 항상 세션 값 - `plan_search`는 문서화된 의도적 override라 그대로 둠(아래 참고) |
| ✔ 에이전트 결함2 전수/개수 질의 10건 절단 | 완전 수정 | wide(exhaustive/negative/count) 질의는 `document_ids` 무절단, `hits`는 문서 단위로 자르고 총 글자 수 예산 하나 더 |
| 도구 호출마다 `PipelineStore` 새로 열기 | 완전 수정 | `_LazyPipelineStore` - 도구 세트(`default_tools()` 호출 1회) 수명 동안 하나. `delegate_task`는 자기 몫을 직접 닫음. `run_query`/`AgentSession`(agent/runtime.py)의 종료 시점 close는 아직 안 걸림(아래 "남은 것") |
| ✔ 에이전트 결함3 깨진 JSON이 루프를 죽임 | 완전 수정 | `openai_compatible.py`가 파싱 실패를 `INVALID_JSON_ARGUMENTS_KEY` 센티널로 바꾸고, `loop.py::_dispatch`가 그걸 걸러 도구 오류 결과로 |
| 필수 인자 누락 메시지(`error: 'query'`) | 완전 수정 | `error: missing required argument 'query'` |

### 애매한 경우에서 고른 행동 ("지난 12일")

"지난 12일"은 "최근 12일"(기간)로 해석하지 않고 **그대로 둔다**(시간 필터 없음, 문자열은
semantic_query에 남는다). 근거: 한국어에서 "지난 N일"은 거의 항상 "(어느 달의) N일"이라는
날짜를 가리키지 "최근 N일간"을 뜻하지 않는다("최근 12일"은 자연스럽지만 "지난 12일"을
기간으로 읽는 화자는 드물다) - 그런데 어느 달인지 확정할 근거가 텍스트에 없다. 날짜로
해석(예: "이번/지난달의 12일")하는 옵션도 검토했지만, 그 추정이 틀렸을 때 좁게 걸리는
시간 필터가 정답 문서를 조용히 빼는 게 필터를 안 거는 것보다 비용이 크다고 판단해 **미해석**
쪽을 택했다(`temporal.py::_recent_amount`의 판단 - "최근"만 일 단위 수량의 기간 해석을
허용하고 "지난"/"저번"+일 단위는 배제). "지난 12일" 자체가 실제 질의에서 흔한 패턴인지도
불확실해 과적합 위험이 낮다고 봤다.

### 남겨둔 것 / 트레이드오프

- **"socket.io" 수정은 TLD 화이트리스트에서 "io" 전체를 뺀 것**이다(개별 질의 패치가
  아니다) - "실제 .io 메일 발신 도메인이 코퍼스에 없다"는 가정에 기댄다. 이 가정이
  틀리면(예: 누군가 `xxx@company.io`로 메일을 보냈으면) 그 발신 도메인 필터가 조용히
  깨진다. 코퍼스 전수 확인은 안 함(범위 밖) - 재현 필요하면 `sources.metadata->>'participants'`
  에서 `.io` 도메인 존재 여부를 확인할 것.
- **`plan_search`의 `project_id` override는 그대로 뒀다.** 리뷰가 명시한 대상은
  vector/document/lexical/sql_search 4개였고, `plan_search`는 스키마 설명("생략하면 세션
  기본 project_id를 쓴다")과 기존 테스트(`test_default_tools_plan_search_query_project_id_overrides_default`)
  가 override를 **의도된 기능**으로 못 박고 있어 범위 밖으로 판단했다. `search`/`vector_search`
  등의 버그는 검증 없는 값이 세션 값을 **조용히** 이기는 것이었지, override 자체가 문제가
  아니었다 - `plan_search`는 문서화돼 있다는 점이 다르다.
- **`file_search`는 건드리지 않았다.** `project_id`가 스키마에 있지만 애초에 세션
  기본값을 받지 않는 도구라(`default_tools()`가 `_make_file_search_tool()`에 `project_id`를
  넘기지 않는다) "세션 값을 덮어쓴다"는 버그 형태 자체가 성립하지 않는다.
- **`run_query`/`AgentSession`이 도구 세트의 `aclose()`를 아직 안 부른다** -
  `agent/runtime.py`/`cli.py`는 이 fix의 소유 파일이 아니다(파일 소유권 표 참고).
  CLI 한 번 실행에 한정된 프로세스라 지금도 안전하지만(프로세스 종료 시 OS가 소켓을
  정리한다), 인터랙티브 REPL을 오래 켜 두는 시나리오나 같은 프로세스에서 세션을 여러
  개 만드는 시나리오에는 `finally`로 `session._tools.aclose()`(또는 동등한 것)를 거는 한
  줄짜리 후속 작업이 필요하다. `delegate_task`는 이 fix 안에서 자기 몫을 직접 닫으므로
  영향 없음.
- **랭킹 구조 결함(1·2번, RRF 문서 단위 병합·전수 질의 anchor 붕괴)은 이번 범위가
  아니다** - 계획서(`drifting-puzzling-eagle.md`) S2, 다른 파일(`rerank.py`/`retrieve.py`)
  소관.

### 회귀 검증 레시피

`uv run --extra dev --extra hwpx --extra xlsx --extra pptx --extra slack --extra google
--extra notion --extra korean pytest tests/search tests/agent tests/test_cli.py -q` -
402 passed / 18 skipped(스킵은 전부 `KF_TEST_DATABASE_URL` 필요한 실 DB 테스트, 이번
세션에선 의도적으로 안 켬). `ruff check src tests`/`mypy src` 둘 다 통과.

리뷰의 구체 probe 재현(before → after, `now`는 리뷰와 동일하게 2026-09-14 기준 사용):
- "지난 4월 회의록" → `최근 4개월`(2026-05-01~) → `2026년 4월`(2026-04-01~05-01, KST)
- "지난 오월 워크숍" → `최근 5개월` → `2026년 5월`
- "지난 12일 지출" → `최근 12일` → 미해석(None)
- "작년 3분기에 김민수가 보낸 메일" → `2026-07-01~2026-10-01` → `2025-07-01~2025-10-01`(KST)
- "Q1 2025 보고서" → `2026년 Q1` → `2025년 Q1`
- "체크리스트 문서" → `wants_exhaustive=True` → `False`
- "전문가 의견" → `wants_whole_document=True` → `False`
- "김철수님이 보낸 메일" → `authors=['김철수님']` → `authors=['김철수']`
- "socket.io 연결 오류" → `participants=['socket.io']` → `participants=[]`
- 깨진 JSON 도구 인자(Qwen 관측 형태) → `JSONDecodeError`로 `run_agent` 전체 죽음 → `error: invalid JSON arguments: ...` 도구 결과로 루프 생존
- exhaustive 질의 130건 → `document_ids` 10~50개로 절단 → 130개 전부, `predicted_count`와 일치
