# Enterprise Knowledge Search 아키텍처 (설계 문서)

`agent-search.md`가 `file_search`/`vector_search`/`graph_search` 세 검색 도구의 설계를 다룬다면,
이 문서는 그 위/주변에 필요한 **더 넓은 RAG 파이프라인**(Query Understanding부터 Evidence
Grounding까지)을 레이어별로 정리한다. 실제 프로덕션 엔터프라이즈 검색 엔진이 갖추는 레이어를
기준으로, 이 저장소에 무엇이 실제로 있고 무엇이 없는지, 없는 것은 왜 아직 안 만들었는지를
명시한다 — `roadmap.md`/`agent-search.md`가 이미 세운 "설계는 막지 않는다, 스코프 관리로
미룬다" 원칙을 그대로 따른다.

**상태 요약**:

| 레이어 | 상태 |
|---|---|
| ① Query Understanding (intent/entity/temporal) | **부분 구현** — `search/constraints.py`가 질의를 구조화된 제약(`QueryConstraints`: 시간 범위/작성자/식별자/문서 참조/소스·문서 타입)과 의도 플래그(latest/exhaustive/count/summary/whole_document/negative/comparison/multihop)로 분해하고 `suggest_strategies()`로 검색 전략 이름을 내놓는다(규칙 1단계 + 선택적 LLM 보강 2단계). temporal parsing(`search/temporal.py`)을 재사용. 그 전략 목록을 실제로 실행해 하나의 랭킹으로 합치는 실행기가 `search/retrieve.py`(아래 "Retrieval Plan 실행기" 절, 에이전트 `search` 도구가 씀). 아직 없는 것: conversation-aware 질의 복원, 동의어/약어 확장 |
| ② Query Planning (multi-hop 분해) | **구현됨** — `search/planner.py`, 아래 절 참고 |
| Query Routing (소스 선택) | **구현됨** — `search/router.py`, 아래 절 참고 |
| ③ Hybrid Retrieval (BM25+Vector+Metadata+Graph) | **부분 구현** — keyword(`file_search`)+semantic(`vector_search`)+metadata(`project_id`/`type`/시간 범위) 병행 실행 + RRF reranking까지 있음. 문서 제목/원문 청크의 pg_trgm 렉시컬 검색(`document_search`/`lexical_search`)도 있음. Graph 전용 API(`graph_search`)는 미구현이지만 `sql_search`로 `relations` 테이블을 직접 JOIN하는 것으로 상당 부분 대체 가능 |
| Multi-stage Retrieval / Reranking | **부분 구현** — `search/rerank.py`의 Reciprocal Rank Fusion, 아래 절 참고. Cross-Encoder/LLM reranker는 미구현 |
| ④ Context Engineering (dedup/conflict/compression) | **부분 구현** — 토큰 예산 압축(`agent/compaction.py`), cross-source dedup(`search/rerank.py`, knowledge_object_id 기준)은 있음. conflict 탐지/source reliability/temporal consistency/evidence clustering은 없음 |
| ⑤ Map-Reduce / Hierarchical Retrieval | 설계만, 미구현 |
| ⑥ Temporal Search (자연어 시간 표현 → 범위 필터 / Text-to-SQL) | **구현됨** — `search/temporal.py`(자연어→범위 필터)와 `search/sql.py`(Text-to-SQL, `sql_search` 도구)로 시간 범위와 속성값 조건을 SQL WHERE/JOIN 하나로 결합하는 temporal join까지 가능해졌다. 아래 절 참고 |
| Query Routing 확장 (SQL/Web 소스) | **부분 구현** — SQL 소스는 `sql_search`로 추가됐다(단, `search/router.py::route_query`의 자동 라우팅 대상에는 아직 없음 - 모델이 직접 판단해서 호출). Web 소스는 여전히 미구현 |
| Evidence Grounding / Citation | **약하게 구현** — 시스템 프롬프트가 path/knowledge_object_id 인용을 지시할 뿐, claim-level 매칭/인용 파이프라인은 없음 |
| Search Evaluation / Feedback Loop | **재구현됨 (2026-09-14)** — grain(`chunk`/`knowledge_object`)을 고정한 자동 생성 골든셋 + retrieval 지표(기본) + 에이전트 답변 채점(`--e2e` 옵션), 결과는 파일. 사람 피드백 로깅·온라인 groundedness gate는 없다 — [`evaluation.md`](evaluation.md) 참고 |
| Iterative / Agentic Retrieval | **이미 구현됨**(암묵적 판단) — 아래 절 참고. 명시적 groundedness gate로 강화하는 안은 설계만 남아 있다(평가 하네스 자체는 재구현 예정) |

---

## ① Query Understanding — **부분 구현**, `search/constraints.py`

Intent classification(사실 조회/비교/요약/탐색/분석), entity extraction(사람/프로젝트/문서),
query rewriting/expansion(모호한 질문 → 검색 친화적 질의, 동의어/약어 확장), conversation-aware
query("그 프로젝트는?" → 이전 대화의 entity 복원)까지 포함하는 넓은 레이어. 이 중 **intent
플래그 + 구조화된 제약 추출 + 벡터용 질의 재작성**을 `search/constraints.py`가 명시적 레이어로
담당한다. conversation-aware 복원과 동의어/약어 확장은 여전히 에이전트(`agent/prompts.py`)가
맡는다.

### 왜 지금 명시적 레이어로 뽑았나

원래 이 문서는 "에이전트 자신의 판단이 이 레이어의 상당 부분을 공짜로 해주므로 별도 전처리를
만들면 중복"이라고 적고, 뽑아낼 시점을 (a) 에이전트 turn 없이 검색을 호출하는 소비자가 생길 때,
(b) LLM 판단만으로는 부정확한 구조화된 파싱이 필요할 때로 못 박아 두었다. 둘 다 왔다.

- (a) **평가 하네스 = 에이전트 없는 소비자.** 골든셋 기반 IR 지표(Recall/MRR/NDCG)는
  "질의 → 검색 결과"를 LLM 판단 없이 같은 입력에 같은 출력으로 재현해야 지표가 의미를
  가진다. 에이전트가 매 턴 암묵적으로 하던 "이건 특정 문서 요약이다 / 작성자
  필터다 / 개수 질문이다" 판단을 결정론적 함수로 꺼내지 않으면 평가할 대상 자체가 없다
  (이 평가 하네스는 2026-09-14 삭제, 재구현 예정 — [`roadmap.md`](roadmap.md)).
- (b) **검색 유형 표의 절반은 유사도 검색으로 원리적으로 못 잡는다.** `query-types.md`의
  20개 유형 중 Navigational/Aggregation/Set Query/Negative/Latest/Version/Whole-document Summary는
  임베딩 유사도가 아니라 제목 조회, COUNT, exhaustive 수집, 정렬, 문서 전체 읽기가 필요하다.
  전략을 고르려면 질의를 먼저 제약+플래그로 분해해야 하고, 그 분해는 `temporal.py`가 이미
  증명했듯 대부분 기계적이다.

### 규칙 1단계 / LLM 2단계로 나눈 이유

- `extract_constraints_rules(query, now=)` — LLM 없이 regex/마커 사전만으로. 시간 범위는
  `search/temporal.py`를 그대로 재사용(올해/작년만 이 모듈이 보충), 따옴표/에러코드/파일명/
  `2025_SH_업무_보고서`형 연결 토큰, 한국어·영어 의도 마커(최신/전부/개수/요약/전체/부정/비교/
  multi-hop 추정), 작성자 조사 패턴("X가 작성한", "from X"), 소스·문서 타입 힌트. 결정론적이라
  평가 기준선이 되고 단위 테스트(`tests/search/test_constraints.py`, 유형별 30여 질의)로 못
  박힌다. `router.py`/`temporal.py`와 같은 recall 우선 원칙 - 플래그 오탐의 비용은 전략 하나를
  더 도는 정도다.
- `extract_constraints(query, provider=)` — `ChatProvider`가 주어지면 규칙 결과를 프롬프트에
  실어 보내 **entities/authors/document_reference/document_types/semantic_query/is_multihop/
  comparison_targets만** 보강한다. 시간 범위·식별자·의도 플래그는 규칙이 확정한 것이라 LLM이
  덮어쓸 수 없다(병합 정책은 `merge_llm_enrichment` docstring) - LLM이 "2025년 3월"을 다른 달로
  옮기거나 에러코드를 "정정"하는 사고를 구조적으로 막기 위해서다. JSON 파싱 실패/예외 시 규칙
  결과로 degrade(`planner.py::decompose_query`와 같은 태도). 각 필드의 `origins`(rule|llm)를
  남겨 평가 시 "LLM 보강이 실제로 무엇을 바꿨나"를 집계할 수 있다.
- `ChatProvider.send()`는 temperature를 노출하지 않으므로 결정성은 "JSON 하나만" 프롬프트와
  병합 정책으로 확보하고, temperature=0은 provider 구성 쪽 책임으로 둔다.

### 전략 이름 계약 (`suggest_strategies()` → `search/retrieve.py`)

통합 실행기 `search/retrieve.py`(아래 절)는 이 이름 목록을 받아 실제 검색을 조합한다.
앞쪽은 후보 생성, 뒤쪽은 후처리/집계이며 각 그룹 안은 우선순위 순이다.

| 전략 | 켜지는 조건 | 의미 |
|---|---|---|
| `document_lookup` | `document_reference` 또는 `identifiers` | 문서 제목/파일명 조회(Navigational) - 다른 에이전트가 만드는 `document_search`가 이걸 실행 |
| `lexical` | `identifiers` 또는 `quoted_phrases` | 본문 정확 일치(Exact Match) - 임베딩은 에러코드에 의미가 없어 못 잡음 |
| `metadata_filter` | authors/participants/source_types/document_types/time_range 중 하나 | `sources.metadata`/타임스탬프 WHERE 필터. 단독 랭킹이 아니라 다른 전략과 결합 |
| `vector` | `semantic_query` 비어 있지 않음 | 제약 표현을 걷어낸 재작성 질의로 임베딩 검색 |
| `keyword_arm` | `vector`와 동일(항상 짝) | 같은 `semantic_query`를 제목·본문에도 거는 어휘 arm. 라우팅에서는 절대 갈라지지 않고, `strategies_override`로 A/B 할 때만 한쪽을 끈다 |
| `latest` | `wants_latest` | 소스 타임스탬프 내림차순 상위 N |
| `exhaustive` | `wants_exhaustive` 또는 `is_negative` | top-k 절단 없이 조건 만족 전부 수집 |
| `count` | `wants_count` | 후보 개수가 답 - 필터 조건에서 SQL COUNT 직행 가능 |
| `whole_document` | `wants_whole_document` (전문/원문 마커, 또는 문서 참조+요약) | 청크가 아니라 `read_document`로 본문 전체 |
| `comparison` | `is_comparison` | `comparison_targets`별 독립 검색 후 나란히 |
| `negative` | `is_negative` | 대상 집합을 exhaustive로 모은 뒤 부재 판정 - 유사도는 "없음"을 증명 못 함 |
| `multihop` | `is_multihop` | `search/planner.py`로 분해 실행 |

아직 없는 것: conversation-aware 질의 복원, 동의어/약어 확장, `route_query()`와의 통합(router는
sub-query용 2-소스 선택기로 그대로 둔다).

### Retrieval Plan 실행기 (`search/retrieve.py`) — **구현됨**

`retrieve(query, *, store, provider=None, project_id=None, now=None, limit=10, use_llm_constraints=True,
strategies_override=None) -> RetrievalResult`. 위 전략 이름 목록을 실제 검색 호출로 바꿔 **하나의
랭킹**으로 합치는 함수이며, 에이전트 루프 없이 호출된다.

**왜 필요했나.** 소비자가 둘이고 둘 다 "어떤 도구를 어떤 순서로 부를지"를 LLM 판단에 맡길 수 없다.
(1) 골든셋 IR 평가 하네스(에이전트 턴 없이 질의 → `document_ids` 랭킹을 얻어야 하고, 같은
입력에 같은 출력이어야 지표 비교가 성립하는 소비자 — 이 하네스 자체는 2026-09-14 삭제,
재구현 예정. [`roadmap.md`](roadmap.md) 참고). (2) 에이전트에게 도구가 7개(document/lexical/vector/sql/read_document/plan/file)로
늘면서 "이 질의엔 뭘 먼저?"를 매 턴 다시 추론하는 비용과 편차가 커졌다 - `search` 도구
(`agent/tools.py::_make_search_tool`)는 이 실행기를 그대로 감싼 것이라, 실행기가 좋아지면
평가 지표와 에이전트 경험이 같이 좋아진다(분리돼 있으면 둘이 따로 논다). LLM은 (a) 제약 보강
(`extract_constraints`), (b) 멀티홉 분해(`plan_and_search`)에만 쓰이고 `provider=None`이면 둘 다
건너뛴다 - 결정론적 기준선.

**반환 계약**: `RetrievalResult{query, constraints
(QueryConstraints.to_dict()), strategies, steps: [RetrievalStep{strategy, source, n_hits, detail}],
hits(각 hit에 document_id/target_kind/score 필수), document_ids(hits에서 첫 등장 순 중복 제거 -
채점 단위), scores(문서별 최고 점수), predicted_count(wants_count일 때), documents_read
(read_document한 문서), elapsed_ms}`. `steps`는 사람이 실패 원인을 추적하는 용도다 - 어떤 SQL을
돌렸는지, anchor를 왜 못 잡았는지, vector가 왜 비었는지가 전부 `detail`에 남는다.

**전략별 실행 규칙**:

| 전략 | 실행 | 비고 |
|---|---|---|
| `document_lookup` | `document_reference`/`identifiers`/`quoted_phrases` 각각 `document_search` | 문서 단위 최고 점수가 1.0(제목 substring)이고 2위와 0.2 이상 벌어지면 그 문서가 **anchor**. 2위도 1.0이면("AGENTS.md" vs "agents-skills-AGENTS.md") ambiguous - 후보 전부 히트로 |
| `lexical` | `identifiers`/`quoted_phrases` 각각 `lexical_search` | anchor가 있으면 그 문서 히트만 남김(하나도 없으면 좁히지 않고 `anchor_narrowing="no_hits_in_anchor"` 기록) |
| `metadata_filter` | authors/participants/source_types/document_types/time_range를 `sources`/`documents` WHERE로 - `execute_readonly_query`(READ ONLY + 강제 롤백) | 값은 `'`→`''` 이스케이프로 인라인(스토어가 문자열만 받음), SQL은 detail에. 작성자/참여자는 `sources.metadata->>'participants'` 텍스트 ILIKE + `documents.metadata->>'author'`(개발 DB의 gmail이 `participants`만 갖고 `from`이 없어서), 문서 타입은 `documents.title ILIKE`("메일"은 제목이 아니라 gmail 소스 타입으로 해석). 상한 500행 |
| `vector` | `semantic_query`로 `vector_search`(since/until 전달) | 임베딩 0행/임베더 미구성이면 빈 결과 + `error` 기록(예외 아님). 유사도가 `KF_VECTOR_MIN_SCORE`(기본 0.45) 미만인 히트는 버리고 `dropped_below_min_score`에 기록 - ANN에는 관련도 하한이 없어 "관련 문서 없음"이 정답인 질의도 후보가 찬다 |
| `keyword_arm` | 같은 `semantic_query`를 `document_search`(제목) + `lexical_search`(본문)에, **`vector` 바로 다음에** | `document_search#semantic` + `lexical_search#semantic` 소스로 RRF에 별도 투표자로 합류. 제목 1.0 일치면 anchor도 잡는다(`document_lookup`과 공유). 순서가 계약이다 - `_claim_anchor`는 먼저 잡힌 anchor를 덮어쓰지 않으므로 arm을 vector 앞으로 옮기면 지표가 움직인다. 둘 다 빈손이면 `_term_fallback`(아래) |
| `comparison` | `comparison_targets` 각각 `document_search`(대상 안의 시간 표현은 그 대상에만 적용), 없으면 `lexical_search` | 대상별 최상위 문서를 anchor 다음으로 최상위 고정 |
| `multihop` | provider 있으면 `plan_and_search` 근거를 한 소스로 RRF 합류 | 없으면 `skipped="no_provider"`. document_id 없는 히트(OKF file_search)는 채점 단위가 없어 버림 |
| `latest` | **관련도 상위 `KF_LATEST_RELEVANCE_POOL`(기본 5) 문서 안에서만** `content_at` 내림차순, 같은 `version_family`는 `version_sort_key`로 최신 하나만 남기고 나머지는 뒤로 | family 안에서는 버전 표기(최종 > vN > 무표기 > 초안)가 시각보다 우선 - 구버전을 나중에 다시 올린 경우 때문. pool 밖 후보는 관련도 순 그대로 뒤에 붙는다 - 후보 전체를 시각 정렬하면 "질의와 상관없이 가장 최근 문서"가 1위가 된다(아래) |
| `exhaustive`/`negative` | 소스별 limit을 200으로, 최종 컷오프 없음 | 부재 판정은 LLM 몫 - 실행기는 후보만 |
| `count` | `predicted_count` = 최종 문서 수. 필터 단독 결과면 같은 WHERE로 `SELECT COUNT(*)` | count도 exhaustive 수집을 함의 |
| `whole_document` | anchor(없으면 상위 2개 문서)를 `read_document`로 읽어 `documents_read`에, 청크를 문서 순서대로 히트 맨 앞에 | 같은 청크를 가리키던 검색 히트는 중복이라 제거 |
| (grain 펼치기) | 호출자가 `grain`을 **명시**했을 때만. limit 절단 뒤 grain 히트(`chunk_id`/`knowledge_object_id`)가 없는 문서마다 `vector_search(document_ids=[문서])`로 그 문서의 grain 단위 3개를 질의 유사도 순으로 골라 문서의 첫 히트 바로 뒤에 끼움. step `grain_expansion` | 2026-09-14. 제목·필터 히트는 문서 id만 싣고 anchor 좁히기가 벡터 히트를 지워서, 정답 문서를 1위로 찾고도 grain 채점 리스트가 비었다(평가 스모크). 문서 순서·`scores`는 그대로, 새 문서는 안 데려옴, `vector_min_score` 하한 없음(문서는 이미 뽑혔다). `grain=None`(설정 grain)은 펼치지 않는다 - 제품 경로는 문서 단위 히트로 충분하고 문서당 조회 비용을 낼 이유가 없다. 문서 id 필터는 btree + 정확 정렬이라 1ms |

**어휘 arm (`keyword_arm`)**:

`semantic_query`를 제목·본문에도 거는 하이브리드의 어휘 쪽 팔이다. 원래는 vector가 빈손일 때만
도는 degrade 경로였고(임베딩 0행 배포용), 상시 arm으로 승격한 경위는 골든셋 다섯 번째
실측에서 나왔다(그 평가 하네스는 2026-09-14 삭제, 재구현 예정). 두 단계로 돈다.

1. **전체 질의**: `semantic_query` 문자열 그대로 `document_search`(제목) + `lexical_search`(본문).
2. **term 분해** (`_term_fallback`) — 1단계가 **양쪽 다 빈손일 때만**. `keyword_terms`가 질의를
   term으로 쪼개 term마다 `lexical_search`를 부르고, 결과를 **소스를 나눠**
   (`lexical_search#<term>`) RRF에 넣는다.

왜 term으로 쪼개나: `lexical_search`/`document_search`의 매칭은 pg_trgm 위의 3단(전체 질의
substring → 모든 토큰 substring → `word_similarity`)이다. "농협 MVP 투입 인력 명함 제작 논의"
같은 자연어 질의는 세 단계가 전부 실패한다 — 그 문자열을 통째로 담은 청크도, 일곱 토큰을 전부
담은 청크도 없고, 질의가 길어 `word_similarity`도 임계값 아래로 떨어진다. v0 baseline에서
conversation_thread(0.250)/sequence(0.343)가 낮았던 실제 원인이 이것이고, `steps`에 `n_hits: 0`
으로 남아 있었다(handoff가 짐작했던 `document_links` 미사용이 아니다).

**소스를 term별로 나누는 것이 핵심**이다: RRF는 같은 히트가 여러 소스 리스트에 등장할수록 점수를
더하므로, 나누면 "질의의 term을 더 많이 담은 문서"가 자동으로 위로 온다. 한 리스트에 몰아넣으면
그 신호가 사라진다.

세 개의 안전장치가 붙어 있다. 전부 실측으로 필요성이 확인된 것이다(스윕 표는 삭제된
평가 하네스의 산출물이었다 — 재구현 예정, [`roadmap.md`](roadmap.md) 참고):

- **coverage 임계값** (`KF_TERM_COVERAGE_RATIO` 기본 2/3, `KF_TERM_COVERAGE_MIN` 기본 2):
  질의의 term을 `ceil(term수 × ratio)`개 이상 담은 문서만 후보로 남긴다. 없으면 term 하나만 스친
  문서까지 전부 후보가 되어, "관련 문서 없음"이 정답인 질의(null/negative)가 전부 무언가를
  돌려주고(`none_correct` 1.000 → 0.000) 개수 질의가 부풀었다(`count_match` 0.500 → 0.000).
- **변별력 게이트** (`KF_TERM_RARE_DF_MAX` 기본 0, `_required_terms`): 위 임계값과 **AND**로
  걸린다. 질의에서 문서 빈도(df)가 **가장 낮은 term(동점이면 전부)** 을 담고 있어야 후보로
  남는다. 개수만 세는 임계값은 코퍼스가 커질수록 반드시 무너지기 때문이다 — 흔한 term끼리
  우연히 겹치는 문서는 코퍼스 크기에 비례해 늘어난다. 실제로 **코드 변경 없이** 263 → 326
  문서(24% 증가)만으로 `none_correct`가 1.000 → 0.667로 떨어졌다("Kubernetes 클러스터 구성
  가이드"가 흔한 `클러스터`/`구성`/`가이드` 세 개만 겹친 무관한 문서를 물어 왔다). df=0인
  term(코퍼스에 아예 없는 개념)은 어떤 문서도 담을 수 없으니 **전부 기각**이 된다 — 의도한
  동작이다. `KF_TERM_RARE_DF_MAX`는 최소 df 위에 얹는 절대 하한이고, 음수면 게이트를 끈다.
- **1단계가 빈손일 때만 실행**: 질의 문자열이 통째로 걸렸으면 그게 term 하나하나보다 강한 신호다.
  항상 돌리면 제목이 정확히 맞은 질의를 term 히트가 밀어낸다(`v0-navigational-002` "codex README":
  ndcg@10 1.000 → 0.356).

term당 조회는 `_TERM_FETCH_LIMIT`(20)으로 따로 막는다 — exhaustive/negative/count 질의는
`fetch_limit`이 200이고 최종 컷오프도 없어서, term 열 개 × 200이 그대로 답에 실리면 재현율보다
정밀도를 더 깎는다.

**df는 term별 히트 수로 근사하면 안 된다.** term마다 `lexical_search`를 이미 부르고 있어 그
히트에서 세면 추가 질의가 0회지만, `search_chunks_lexical`이 `LIMIT`(=20)을
`is_substring DESC, sim DESC, document_id` 순으로 자르기 때문에 **흔한 term일수록 상위 20청크가
같은 문서 몇 개에 몰려** df가 극단적으로 과소평가된다(실측: `인터뷰` 절단 df=3 / 실제 37,
`OCR` 1 / 34, `애자일소다` 1 / 33, `risk-agent` 4 / 51). v0의 per_term 질의 26개 중 **7개에서
"가장 희귀한 term"의 신원이 뒤집혔다** — 판정 자체가 바뀌므로 별도 count가 필요하다. 그래서
`_term_document_frequency_sql`이 term 전부의 df를 **읽기 전용 질의 한 번**(UNION ALL)으로 세고,
그 값과 실측 df의 `max`를 쓴다. `max`인 이유: SQL은 substring 티어만 세어 fuzzy로만 걸린 term을
0으로 보고, 실측은 절단으로 흔한 term을 낮게 본다 — 둘 다 과소평가만 하므로 큰 쪽이 진실에
가깝고, 무엇보다 "히트는 있는데 df=0이라 전부 기각"이라는 모순(오타/동의어 질의가 통째로 빈손이
되는 경로)이 생기지 않는다.

**1단계(`lexical_search#semantic`, 질의 문자열 통째)에는 같은 취약점이 없다.**
`search_chunks_lexical`의 3티어 중 1·2티어는 "질의 전체가 한 청크의 substring" /
"질의의 모든 토큰(최대 8개)이 **한 청크 안에** 전부 있음"이라 **흔한 term만으로는 성립할 수
없고**(희귀 term도 반드시 포함해야 한다), 3티어는 질의 전체에 대한 `word_similarity ≥ 0.5`라
개수가 아니라 관련도 하한이다. 즉 "코퍼스가 커지면 우연한 겹침이 늘어난다"는 메커니즘이 여기엔
적용되지 않는다. 실측으로도 v0의 none 질의 6개 전부 이 경로가 limit=200에서도 0히트다(그래서
term 분해로 내려간 것이다). 게이트를 여기까지 확장하지 않은 이유다.


**`latest`가 관련도를 버리지 않게 하는 이유**:

`latest`는 원래 병합 결과 **전체**를 `content_at` 내림차순으로 재정렬했다. 후보가 2~7개였을
때는 우연히 맞았지만, term 단위 fallback으로 후보가 90~120개가 되자 정답이 무관한 최근 슬랙
메시지 뒤로 밀려 `v0-latest-001/002`가 ndcg@10 0.000이 됐다. **후보 수가 늘수록 나빠지는**
종류의 버그다 — 검색이 좋아지면 이 후처리가 더 망가진다.

그래서 시각 정렬 대상을 **관련도 상위 pool**로 좁혔다. "가장 최근 X"는 "가장 최근 문서"가
아니라 "X에 해당하는 것들 중 가장 최근"이므로, 관련도가 범위를 정하고 시각이 그 안에서 고르는
게 맞다. pool 5는 v0 스윕값이고(7 이상이면 latest 0.544 / version 0.947로 되돌아간다) 근거가
질의 10개뿐이라 잠정값이다.


**병합 결정과 이유**:

- **RRF**(`search/rerank.py`): 소스별 점수 척도가 다르다(제목 tier 1.0/0.5+sim/2, trigram, 코사인).
  planner가 이미 같은 이유로 RRF를 쓰고, 여기서 새 척도를 만들 이유가 없다.
- **anchor 최상위 고정**: 사용자가 파일명을 그대로 적었다면 그 문서가 답의 범위다 - RRF가 다른
  소스의 다수결로 이걸 밀어내면 Navigational/Specific-document QA가 무너진다. 고정은 순서를 지키는
  partition이라 나머지 랭킹은 그대로다.
- **metadata_filter 교집합**: 필터는 랭킹 신호가 아니라 범위다(작성자/기간이 틀린 문서는 아무리
  유사해도 오답). 그래서 RRF 점수에 섞지 않고 병합 뒤 교집합으로 건다. **단 필터를 얼마나 믿을지는
  필터 안에 든 술어의 종류로 가른다** - 아래 표. `applied` 값은 `metadata_filter` step의 detail에
  그대로 남아서 실패를 추적할 수 있다.

  | 상황 | `applied` | 결과 |
  |---|---|---|
  | 필터 질의가 0행 | `filter_empty_ignored` | 필터 무시, 관련도 랭킹 그대로 |
  | 후보 생성 전략이 하나도 히트 없음 | `filter_only` | 필터 집합을 최신순으로 - "지난주 김철수 메일"은 필터만으로 답이 되는 질의다 |
  | 시간 범위를 풀고 다시 돈 arm이 후보를 찾음(아래) | `time_range_relaxed` | 필터 무시 - 이 경우 필터는 시간(+출처 힌트)뿐이다 |
  | 필터가 **어휘 힌트뿐**(`source_type IN (...)` 하나) | `source_hint_not_applied` | 필터 무시, 관련도 랭킹 그대로 |
  | 그 밖에 교집합이 있음 | `intersected:N->M` | 교집합 |
  | 그 밖에 교집합이 0 | `no_overlap_fallback_to_filter` | 필터 집합을 최신순으로 |

  **어휘 힌트뿐인 필터를 하드 필터에서 뺀 이유**(`_is_source_hint_only_filter`): `source_types`는
  `SOURCE_TYPE_MARKERS` 정규식이 질의 표면 어휘만 보고 찍은 추측이고 - LLM 보강 대상 필드도 아니다
  (`LLM_ENRICHABLE_FIELDS`) - 그 어휘 → source_type 매핑은 방향이 자주 반대다. 개발 코퍼스 실측으로
  본문에 "Google Drive"/"드라이브"를 언급하는 문서의 source_type은 **gmail 11건 / slack 4건 /
  google_drive 3건**이었다. Drive에 저장된 파일은 본문에 "Google Drive"라고 쓸 이유가 없고, Drive
  공유요청 알림 메일은 항상 쓴다. 반대로 작성자/참여자/시간/제목 종류는 질의가 명시한 구조적
  술어라 교집합이 0이어도 필터 쪽을 믿는다(자세한 근거는 삭제된 평가 하네스의 회귀 기록에
  있었다 — 재구현 예정, [`roadmap.md`](roadmap.md) 참고).
- **시간 범위는 결과를 0건으로 만들지 못한다** (`_Run.relax_time_range`, 2026-09-14). 질의 속 날짜는
  문서 시각이 아니라 내용을 가리키는 경우가 흔하다("2022년에 종료된 계약의 종료일은?", "이번 주 해야 할
  업무는?", "2026년 9월 1일에 어떤 회의가 예정되어 있나요?" - 평가 스모크에서 since/until이 모든 arm을
  0건으로 만들었다). 그래서 범위 안에서 후보 arm(document_lookup/lexical/vector/keyword_arm/comparison)이
  **전부** 빈손이면 since/until을 풀고 arm을 한 번 더 돌린다(step `time_range_relaxed`). 범위 안에서
  무엇이든 찾으면 지금처럼 하드 필터라 "지난주 회의록"류는 그대로다. 풀고 찾은 후보는 어차피 범위 밖이라
  (범위 안이었으면 첫 판에 걸렸다) 범위 안을 앞세우는 재정렬은 두지 않았다. 안 푸는 경우: arm이 아예 안
  돌았거나(제약이 곧 질의 - `filter_only`가 답), 필터에 작성자/참여자/확장자/MIME이 있다(semantic_query가
  찌꺼기라 빈손이 정상, 필터를 믿는 기존 분기가 맞다). 풀고 찾은 후보에 시간 필터를 교집합으로 걸거나
  `no_overlap_fallback_to_filter`로 날짜 집합을 최신순으로 내면 날짜를 되살리는 셈이라 필터를 적용하지
  않는다. 다시 돌아도 빈손이면 예전과 같은 `filter_only`.
- `limit`은 최종 `document_ids` 길이다. 소스별 조회는 3배 over-fetch해서 필터/anchor 좁히기 뒤에도
  limit개가 남게 한다.
- `strategies_override`는 목록에 적힌 전략**만** 돌린다 - 어휘 arm도 `"keyword_arm"`으로 적어야
  돈다. 즉 `["vector"]`가 순수 벡터 baseline이고 `["vector", "keyword_arm"]`이 기본 하이브리드와
  같다. arm이 이름 없이 `vector()` 안에 숨어 있던 동안에는 `["vector"]`가 arm까지 돌렸고, 그래서
  "순수 벡터"라고 기록된 v0 ndcg@10 0.736은 사실 vector+arm의 값이었다. 이름을 갈라 다시 재보니
  기본 라우팅 **0.893** / `vector,keyword_arm` **0.736** / `keyword_arm` 단독 **0.663** /
  `vector` 단독 **0.477**(76질의, `--no-llm`) - 이 코퍼스에서 검색을 지탱하는 것은 어휘 쪽이고
  벡터 단독은 하이브리드의 절반 조금 넘는 수준이다.

**한계**:

- 임베딩이 없으면(개발 DB 기준 0행) `vector`는 항상 비고 어휘 arm만 남는다 - 패러프레이즈/
  개념 질의는 제목·본문에 그 단어가 그대로 있을 때만 잡힌다. term 분해가 "질의의 단어를 여러 개
  담은 문서"까지는 끌어올리지만(v0 factoid 0.497 → 0.969), 단어가 하나도 겹치지 않는 패러프레이즈는
  여전히 못 푼다 - 그건 임베딩이 있어야 한다. 골든셋 v0
  스모크(provider=None, 임베딩 없음): navigational/whole_document_summary/exact_match 각 1건은
  1위, aggregation(`caddy-drift` 6건)은 6건 전부 상위 6 안이지만 predicted_count=8(본문에
  caddy-drift가 언급된 다른 메시지 2건 포함).
- LLM이 없으면 `document_reference`는 규칙(파일명/연결 토큰)으로만 잡히고 "codex README" 같은 자연어
  제목 참조는 vector fallback의 `document_search`에 기댄다. multihop은 아예 돌지 않는다.
- Slack `participants`는 user id라 이름 기반 작성자/참여자 필터가 닿지 않는다.
- `search_documents_by_title`/`search_chunks_lexical`에 document_id 필터가 없어 anchor 좁히기는
  사후 필터(over-fetch 뒤 걸러냄)다 - anchor 문서의 일치 청크가 fetch 범위 밖이면 놓친다.

## ② Query Planning — **구현됨**, `search/planner.py`

`agent-search.md`가 우선순위로 꼽은 두 가지(Query Planning + Query Routing) 중 하나. 멀티홉
질문("지난달 성과가 가장 좋았던 프로젝트와 그 이유는?")을 하위 질의로 분해하고, 서로 독립적인
하위 질의는 병렬로, 앞 결과에 의존하는 하위 질의는 그 결과를 반영해 실행한 뒤 근거 모음을
반환하는 `plan_search` 도구.

```text
"지난달 성과가 가장 좋았던 AI 프로젝트와 그 이유는?"
         │
   decompose_query()  ── 세션을 이미 몰고 있는 ChatProvider로 LLM 호출 1회
         ▼
Q1. 지난달 AI 프로젝트 목록          (depends_on: [])
Q2. 각 프로젝트의 성과 지표          (depends_on: [])
Q3. 가장 성과가 좋은 프로젝트        (depends_on: [1, 2])
Q4. 그 프로젝트의 성공 이유          (depends_on: [3])
         │
   _topological_waves()
         ▼
Wave 1: {Q1, Q2}  ── asyncio.gather로 병렬 실행
Wave 2: {Q3}       ── Q1/Q2 근거 스니펫을 질의에 덧붙여 실행
Wave 3: {Q4}       ── Q3 근거 스니펫을 질의에 덧붙여 실행
         │
   evidence pool 반환 (합성/최종 답변은 하지 않음 - 상위 에이전트 턴이 종합)
```

- **분해**: `decompose_query()`가 `agent/compaction.py`와 같은 패턴(별도 모델/설정 없이, 세션의
  `ChatProvider`로 `send(messages, [], system)` 한 번 더 호출)으로 하위 질의 JSON을 받는다.
  단순 질문은 원 질문 그대로 1개짜리 목록으로 돌아오게 프롬프트에 명시했다 — 억지로 쪼개지
  않는다. LLM 호출/파싱이 실패하면(네트워크 에러, JSON 아닌 응답 등) 원 질문 하나짜리 계획으로
  조용히 fallback한다 — `plan_search`가 최악의 경우에도 "그냥 한 번 검색한 것"보다 나쁘지 않게.
- **의존성 검증**: 모델이 낸 `depends_on`은 신뢰하지 않고 검증한다 — 목록 안에서 **자기 자신보다
  앞선 위치**를 가리키는 것만 유효한 의존성으로 인정하고, 미래 참조/자기 참조/범위 밖 인덱스는
  전부 버린다. 이 덕분에 사이클이 애초에 만들어질 수 없다(`_topological_waves`가 방어적으로
  "더 이상 준비된 항목이 없으면 남은 걸 전부 한 wave로 몰아 반환"하는 안전장치를 갖고 있긴
  하지만, 정상 입력에서는 절대 그 경로를 안 탄다).
- **의존성 반영 방식**: 실제 쿼리 치환/템플릿 언어는 만들지 않았다 — 의존하는 하위 질의의 상위
  근거(summary/snippet/title, 최대 3개)를 원 하위 질의 문장 뒤에 자연어로 덧붙여
  (`"(참고 정보: ...)"`) 그대로 검색한다. 정교한 slot-filling보다 단순하지만, "Q3 결과가 Q4의
  검색 조건이 되는" 상황을 커버하기에는 충분하다고 판단했다.
- **병렬 실행이 실제로 의미 있는 이유**: `agent/loop.py::run_agent`는 한 turn에 여러 tool_call이
  와도 순차 실행한다(`agent-runtime.md`의 "알려진 제약") — 즉 에이전트가 스스로 file_search를
  4번 연달아 호출해도 그건 항상 직렬이다. `plan_search`는 하나의 tool 호출 **안에서**
  `asyncio.gather`로 실제 동시 실행을 하므로, 이 harness에서 유일하게 진짜 병렬 검색이 일어나는
  경로다.
- 최종 답 합성은 하지 않는다 — `{"subqueries": [...], "evidence": [...]}`를 tool_result로 돌려줄
  뿐, 그걸 읽고 실제 답을 종합하는 건 여전히 상위 에이전트 턴(같은 모델)의 몫이다. Map-Reduce
  스타일의 계층적 요약(⑤)이 필요해지는 게 바로 이 지점이지만, 지금은 evidence 개수가 하위
  질의당 `limit_per_subquery`(기본 5)로 작아 굳이 별도 reduce 단계 없이도 모델 컨텍스트에
  들어간다.
- **`limit`/`limit_per_subquery`는 모델이 호출 인자로 직접 지정**하므로(`file_search`/
  `vector_search`도 마찬가지), 정상적인 모델 판단이 아니라 색인된 문서에 심긴 프롬프트
  인젝션이 비정상적으로 큰 값을 요구할 가능성까지 감안해야 한다 — 이 문서 위쪽 "evidence 개수가
  작아 괜찮다"는 전제가 값 자체를 신뢰할 때만 성립한다. 그래서 `agent/tools.py::
  _MAX_SEARCH_LIMIT`(file_search/vector_search, 50)과 `_MAX_PLAN_SEARCH_LIMIT_PER_SUBQUERY`
  (plan_search, 20 — 최대 5개 하위 질의 × 최대 2개 소스에 곱해지므로 더 낮게 잡음)로 서버가
  강제 clamp한다. JSON Schema의 `maximum`은 협조적인 모델에게 주는 힌트일 뿐이라, 핸들러
  (`_clamp_limit()`)에서도 반드시 다시 잘라낸다 — `search/sql.py::DEFAULT_MAX_ROWS`가 이미 쓰던
  "모델이 부른 값을 그대로 믿지 않는다" 패턴과 동일.

## Query Routing — **구현됨**, `search/router.py`

`route_query(query, available_sources) -> RouteDecision`. `plan_search`의 하위 질의마다 어떤
소스(`file_search`/`vector_search`)를 실제로 호출할지 결정한다.

**LLM 호출이 아니라 순수 휴리스틱인 이유**: 상위 에이전트 자신은 이미 LLM이라 `file_search`/
`vector_search`를 직접 호출할 때 스스로 판단할 수 있다(`agent/prompts.py`의 라우팅 힌트) —
router가 필요한 곳은 **프로그램적으로 생성된**(모델 턴이 없는) `plan_search`의 하위 질의뿐이다.
이 판단 하나를 위해 LLM 호출을 추가하면, 잘못 골랐을 때의 비용(소스 하나를 더 부르는 정도 -
결과는 어차피 합쳐서 반환)보다 추가 latency/비용이 더 클 수 있다고 판단했다.

신호:
- 따옴표로 감싼 구문, `ko-abc123`/`PRJ-A`/`#1234` 같은 id 형태 토큰 → 정확 일치에 강한
  `file_search`를 반드시 포함(+ 보강용 `vector_search` 병행).
- 3단어 이하의 짧은 키워드성 질의 → 어느 쪽이 맞을지 모호하므로 둘 다.
- 질문형 어미/의문사(왜/어떻게/무엇/누가/이유/비교 등)나 6단어를 넘는 긴 자연어 문장 →
  의미 검색에 강한 `vector_search`를 우선(+ 보강용 `file_search` 병행).
- 그 외 애매한 경우 → 둘 다, `file_search` 우선.
- `vector_search`가 애초에 구성되어 있지 않으면(`KF_DATABASE_URL` 없음) 항상 `file_search`만.

**결정된 스코프**: 라우터는 "이 소스들을 실행하라"는 목록만 돌려준다 - 실행 순서에 따른
폴백(먼저 것이 0건이면 다음 것)이 아니라, 목록에 있는 소스를 **전부** 병행 실행해서
`search/rerank.py`로 병합한다(아래 ③ 참고) - 소스 선택은 라우터, 병합/재정렬은 rerank 모듈로
역할이 나뉜다.

## Iterative / Agentic Retrieval — **이미 구현됨**(암묵적 판단), 새로 만들 필요 없음

"한 번 검색하고 끝내지 않고, 근거가 부족하면 재검색"이라는 요구사항은 `agent/loop.py::run_agent`
의 도구 호출 루프 자체가 이미 제공한다 — 에이전트는 file_search/vector_search/plan_search 결과를
보고 부족하다고 판단하면 같은 턴 안에서 다른 질의로 다시 호출할 수 있고(최대
`KF_AGENT_MAX_ITERATIONS`회), `write_plan`으로 여러 단계 조사 계획을 스스로 세울 수도 있다
(`agent-runtime.md`). 별도의 "충분한가?" 판단 모듈이나 refinement 루프를 새로 만들지 않은 이유는
이게 이미 tool-calling 에이전트의 기본 동작이기 때문 - 추가로 만들면 에이전트가 이미 하는 일을
어설프게 다시 구현하는 셈이 된다.

"충분한가?"의 판단 자체는 여전히 모델의 암묵적 자기평가에 전적으로 의존한다 - 강제되는 검증
단계는 없다. 답변 확정 직전에 claim-evidence 매칭으로 한 번 더 검증하는 명시적 groundedness
gate는 설계안만 있었으나, 그 설계가 실려 있던 평가 서브시스템이 2026-09-14 전면 삭제됐다 -
2026-09-14 재구현한 평가([`evaluation.md`](evaluation.md))는 오프라인 채점만 하며, 이 gate는 여전히 없다.

## ③ Hybrid Retrieval — 부분 구현

- Keyword(`file_search`, substring/frontmatter) + Semantic(`vector_search`, pgvector) 병행
  실행은 Query Routing을 통해 이미 가능(위 참고).
- Metadata filtering(`project_id`/`type`/시간 범위)은 `file_search`/`vector_search` 둘 다
  1급 파라미터로 지원한다. "김철수가 참여한 Project A 관련 논의"처럼 **entity 필터**가
  필요한 경우 - KnowledgeObject 스키마에 참여자 필드가 정규화되어 있지 않아 `project_id`/`type`
  같은 전용 파라미터로는 못 걸지만, `sql_search`(아래 ⑥ Text-to-SQL 절)로 `knowledge_objects`
  를 `relations`와 JOIN해서 우회할 수 있게 됐다 - 전용 파라미터가 아니라 임의 SQL이라는 점에서
  "구조화된 필터"라 부르긴 이르지만, 실질적으로 이 gap을 메운다.
- Graph 축 전용 API(`GET /relations/expand`, `agent-search.md`)는 여전히 미구현이지만,
  `sql_search`로 `relations` 테이블을 직접 JOIN하는 1-hop 수준까지는 이미 가능(아래 ⑥ 참고).
- **Reranking(RRF) — 구현됨**, `search/rerank.py::merge_and_rerank`. `plan_search`가 하위
  질의 하나에 대해 두 소스를 모두 호출하면, 두 결과 리스트를 Reciprocal Rank Fusion(RRF)으로
  병합한다 - `file_search`의 정수 가중치와 `vector_search`의 cosine 유사도는 스케일이 달라
  직접 합산/평균이 무의미하므로, 각 소스가 이미 매긴 **순위**만 보고 `1/(k+rank)`를 소스별로
  합산하는 방식을 썼다(프로덕션 hybrid search가 흔히 쓰는 표준 기법, Elasticsearch/Weaviate
  등). 동시에 `knowledge_object_id`가 겹치는 hit을 하나로 합치는 **cross-source dedup**도
  이 단계에서 해결된다(`file_search`가 이제 frontmatter의 `knowledge_object_id`를 응답에
  노출한다 - 이전에는 이게 없어 매칭 키가 아예 없었다). `path`도 `knowledge_object_id`도 없는
  hit(다른 소스에서 온, 식별자가 겹칠 수 없는 hit)은 병합하지 않고 그대로 자기 소스의
  순위만으로 점수를 받는다.
  - **아직 없는 것**: Cross-Encoder/LLM 기반 reranker(질의-문서 쌍을 실제로 이해해 재정렬) -
    RRF는 각 소스의 기존 순위를 신뢰하는 저비용 융합일 뿐, 두 소스 모두 낮게 평가한 문서를
    끌어올리거나 실제 관련성을 재판단하지는 못한다. 필요해지는 시점은 RRF로 병합한 상위
    결과의 정밀도가 실사용에서 부족하다고 판명될 때.
- **문서 렉시컬 검색(Document/Lexical Search) — 구현됨**, `search/service.py::document_search`/
  `lexical_search` + `storage/postgres_store.py::search_documents_by_title`/`search_chunks_lexical`,
  agent 도구 `document_search`/`lexical_search`(`KF_DATABASE_URL`이 있을 때만 등록).
  검색 유형 중 Navigational("2025_SH_업무_보고서 찾아줘"), Exact/Identifier("ERR_CONNECTION_RESET
  문서"), Ambiguous("SH 보고서" → 후보 여러 개)는 벡터 유사도로 풀 수 없다 - 파일명/에러 코드는
  임베딩이 "가까울" 의미가 없고, `file_search`는 OKF 마크다운(추출된 KO 요약)만 봐서 요약 과정에서
  사라진 리터럴을 놓친다. 그래서 Postgres의 `documents.title`/`chunks.text`에 대한 문자열 일치
  검색을 따로 뒀다.
  - **왜 tsvector(FTS)가 아니라 pg_trgm인가**: 제목/청크의 대부분이 한국어 + 언더스코어로 이어진
    파일명(`2025_SH_업무_보고서.pdf`)이다. Postgres 기본 텍스트 검색 파서(`simple`)는 한국어를
    형태소 단위로 나누지 못하고 공백 기준으로만 자르며, 언더스코어 파일명은 통째로 한 토큰이 되어
    "SH 보고서"로는 절대 못 찾는다. trigram은 토크나이저가 없어 언어에 무관하고, `ILIKE '%q%'`
    (substring)과 `similarity`/`word_similarity`(오타/부분 일치) 둘 다 같은 GIN 인덱스
    (`gin_trgm_ops`)로 서빙한다. 영문/식별자용 tsvector 병행은 넣지 않았다 - 식별자는 substring
    일치가 정답이고 trigram이 그걸 이미 인덱스로 처리하므로 얻는 게 없다.
  - **3단 랭킹**: ① 질의 전체 substring(`ILIKE`) → ② 공백 토큰 전부 포함(`ILIKE ALL(...)`,
    "SH 보고서"가 `2025_SH_업무_보고서.pdf`에 맞는 단계) → ③ `word_similarity(q, title) >= 0.3`
    (청크는 0.5). `similarity` 대신 `word_similarity`를 쓰는 이유: 짧은 질의가 긴 파일명에 얼마나
    덮이는지를 재므로("SH 보고서" 0.7 vs `similarity` 0.37) 제목 검색이 실제로 묻는 질문과 맞다.
    LIKE 패턴은 `_`/`%`를 이스케이프한다 - 파일명의 `_`가 LIKE 와일드카드라 안 하면
    `2025aSHb업무`도 맞는다. `score`는 ①=1.0, ②=0.5+sim/2, ③=sim으로 [0,1] 안에서 단계와 단조.
  - **제목 파생 우선순위**(`storage/document_title.py`, [`storage.md`](storage.md)): 파서가 읽은
    `document.metadata.title` → 소스 메타데이터 `name`(Drive)/`subject`(Gmail) → Slack이면
    `#channel YYYY-MM-DD` → 첫 Heading → NULL. 개발 DB 기준 `documents.metadata->>'title'`은 전부
    NULL이라(파서는 Drive 파일명/메일 제목을 모른다 - 그건 `sources.metadata`에만 있다) 이 파생이
    없으면 제목 검색 자체가 성립하지 않는다. Heading이 마지막인 이유: Slack/Gmail 컨텍스트 파서가
    만드는 "Slack #C0BG… 스레드" 같은 합성 제목은 사람이 부르는 이름이 아니다.
  - **결과 모양**: `document_search`는 본문 없이 `{target_kind:"document", document_id, title,
    source_type, uri, modified_at, score}` 후보 목록 - "어떤 문서인지 특정"하는 단계용이라 후보가
    여럿이면 전부 돌려주고 모델이 사용자에게 되묻게 한다. `lexical_search`는 `vector_search`의
    chunk grain 히트와 같은 키(`target_kind:"chunk"`, `document_id`, `chunk_id`, `summary`=일치
    지점 주변 500자, `score`)라 `search/rerank.py`가 그대로 병합할 수 있다. 둘 다 `document_id`를
    항상 싣는다 - 골든셋 채점 단위였다(평가 하네스는 재구현 예정).
  - `search/router.py`: `_ID_LIKE_RE`가 못 잡던 숫자 시작/한글 혼합 식별자(`2025_SH_업무_보고서`
    - `_`/`-`로 이어진 3토큰 이상)와 확장자(.pdf/.xlsx/.docx/.pptx/.hwp…) 포함 토큰을
    `has_exact_match_signal()`의 정확 일치 신호로 추가했다. `plan_search`의 하위 질의 라우팅은
    여전히 `file_search`/`vector_search` 두 소스만 고른다 - `document_search`를 planner에 엮는
    건 골든셋으로 효과를 잰 뒤에.
  - **아직 없는 것**: `document_search` 후보를 `vector_search`/`lexical_search`의 `document_id`
    필터로 바로 잇는 파라미터(지금은 모델이 후보를 고른 뒤 `sql_search`나 본문 검색을 이어 부른다),
    Slack 채널 ID → 채널 이름 해석(제목이 `#C0BGQEQG1L6 2026-08-11` 형태로 남는다 - 컬렉터가
    `channel_name`을 메타데이터에 넣으면 파생 로직은 이미 그걸 우선 쓴다).

### 유니코드 정규화(NFC) — 어휘 경로가 구조적으로 못 맞던 버그 (2026-09-04 수정)

`src/knowledge_forge/text_normalize.py`, 커밋 브랜치 `fix/nfc-normalization`.

**정체.** 한글 "관"은 코드포인트 표현이 둘이다 — NFC의 완성형 음절 `U+AD00` 하나, NFD의 자모
분해 `U+1100 U+1161 U+11AB` 셋. 눈으로는 구분이 안 되지만 파이썬 `in`, SQL `ILIKE`, pg_trgm
`word_similarity`에는 완전히 다른 문자열이다. macOS·Google Drive를 거친 파일명은 NFD로 들어오고,
그 파일명이 Drive 공유 알림 **메일 제목**에 박히면 `documents.title` 한 행 안에서 파일명 부분만
NFD, 나머지 한글은 NFC인 **혼합 정규화**가 된다.

실례 (`document_id=410852b07001b87f56a3b1f23e73ab58`):

```
title = 이제 ‘프로젝트 투입 관리_2026-031_한국자산관리공사.xlsx’의 소유자가 되었습니다
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^
             NFD: '한' = U+1112 U+1161 U+11AB (자모 3개)      NFC: '의' = U+C758 (1개)
```

그래서 3단 랭킹의 **① substring(score 1.0) 단계가 통째로 죽고** ③ trigram으로 떨어진다. 파일명이
정확히 일치하는 문서가 0.4286점 9위로 밀리고, 식별자가 **다른** 문서들(`2026-032_동양생명` 2건,
`2026-038-G`/`2026-038-J_농협은행`)이 0.4762점으로 1~4위를 차지했다. 5~8위(`2026-012_현대모비스`,
`2026-023_한화손해보험`, `2026-024_광주은행` 2건)는 정답과 같은 0.4286점인데 tie-break가 제목/
document_id 순이라 정답보다 앞에 왔다.

`retrieve.py`의 anchor 판정이 "1위가 제목 substring 1.0"을 조건으로 삼기 때문에, 이 문서는
anchor도 못 잡는다 — Navigational/Exact 질의에서 어휘 경로가 반드시 맞아야 하는 자리에 정확히
꽂힌 버그다.

**영향 범위(개발 DB 실측).** `documents.title` 4/263행, `chunks.text` 4/936행 — 전부
`source_type=gmail`. 좁아 보이지만 더 중요한 건 상류다: `sources.metadata`의 Drive `name` 77건 +
Gmail `subject` 4건 = **81/502행이 이미 NFD**다. 그 77건은 아직 문서 행이 없어서 제목 통계에
안 잡혔을 뿐, 적재되는 순간 같은 증상이 된다.

**왜 적재·질의 양쪽을 다 고쳐야 하나.** 한쪽만으로는 절반만 맞는다.

| 고친 곳 | 남는 구멍 |
|---|---|
| 저장만 | 사용자가 macOS/Drive에서 복사한 **NFD 질의**를 붙여넣으면 NFC 저장값에 못 맞는다 |
| 질의만 | 이미 저장된 **NFD 행**에 못 맞는다 (마이그레이션 전 데이터, 재적재 안 된 소스) |

그래서 걸어둔 지점은 네 곳이고 전부 같은 `to_nfc()`를 쓴다.

- 적재: `storage/document_title.py::_clean` → `derive_document_title`의 모든 출구, 즉
  `save_document`와 `backfill_document_titles`가 규칙을 공유한다.
- 적재: `PostgresStore.save_chunks`.
- 질의: `PostgresStore.search_documents_by_title` / `search_chunks_lexical` 진입점 — 최하단이라
  `search/service.py`의 도구든 평가 하네스든 store를 직접 쓰는 코드든 전부 덮인다.

`sources.metadata`(수집기가 API에서 받은 원본 기록)와 `documents.elements`(파서 출력)는 일부러
안 건드렸다. 있는 그대로의 기록이라는 게 그 컬럼들의 존재 이유고, **사용 지점**(제목 파생·청크
저장)에서 정규화하면 이미 NFD로 들어와 있는 81개 소스 행도 적재될 때 함께 낫는다.

**왜 NFC이고 NFKC가 아닌가.** `ingest_version.py::family_key_of`는 이미 NFKC+casefold를 쓰고
있어서 선례가 있지만, 그건 화면에 안 나오고 비교에만 쓰이는 **파생 키**다. 전각/반각·기호까지
공격적으로 접어도 잃을 게 없다. 반면 `documents.title`/`chunks.text`는 검색 대상인 동시에
**그대로 사용자에게 보여지는 원문**이라 기준이 다르다.

- 이 버그는 "같은 문자를 다른 코드포인트 열로 적은" 정준 등가(canonical equivalence) 문제다.
  NFC가 정확히 그것만 고치는 최소 수정이다.
- NFKC는 호환 문자까지 접는다: `㈜`→`(주)`, `Ⅳ`→`IV`, 전각 `ＡＢ`→`AB`, `①`→`1`, NBSP→공백.
  국내 문서 파일명에 `㈜`·전각 괄호·로마숫자가 실제로 흔해서, 저장값을 NFKC로 접으면 사용자가
  아는 파일명과 **다른 문자열을 보여주게 된다**(표시 손실). 매칭만 놓고 보면 NFKC도 틀리지 않지만,
  같은 컬럼이 표시 문자열이라 그 대가를 치를 이유가 없다.
- 저장·교환 문자열 표준 권고도 NFC다(W3C Character Model).
- 나중에 호환 문자까지 접는 매칭이 필요해지면 저장값을 바꾸지 말고 **NFKC 파생 키를 따로** 두는
  게 맞다 — `family_key_of`가 그 선례다.

**기존 데이터 마이그레이션.** `knowledge-forge db normalize-unicode`
(`PostgresStore.normalize_unicode_text`). 재적재가 아니라 전용 마이그레이션을 만든 이유:

- 이 행들은 **내용이 멀쩡하고 표현 형태만** 틀렸다. 재적재는 파싱·추출·임베딩(LLM 호출 포함)을
  전부 다시 돌리면서 결국 같은 `document_id`/`chunk_id`를 덮어쓸 뿐이라 대가가 너무 크고,
  Gmail처럼 원본을 다시 못 받는 소스도 있다.
- `db backfill-titles --overwrite`는 제목은 고칠 수 있지만 `chunks.text`는 못 고치고, 제목 파생
  규칙 전체를 재적용하는 훨씬 넓은 연산이다.
- 판정·변환을 파이썬이 아니라 서버의 `normalize(text, NFC)`(PG 13+, 개발 DB는 16.15)로 한다 —
  쓰기 트랜잭션을 짧게 끝내기 위해서다(개발 DB를 다른 프로세스가 동시에 읽는다). 재실행하면
  0건이 나오는 멱등 연산이고, `updated_at`은 안 건드린다(내용 개정이 아니라 표현 정규화).

실제 실행 결과: 문서 제목 4건 + 청크 본문 4건 수정, 재실행 시 (0, 0). 실행 뒤 DB에 남은 비-NFC
행은 `documents.title` 0, `chunks.text` 0.

**수정 전후 (`search_documents_by_title`, 개발 DB 전체 대상).**

| 질의 | 수정 전 | 수정 후 |
|---|---|---|
| `관리_2026-031_한국자산관리공사` | `410852b07001` 0.4286 / **9위**, 1~4위는 `2026-032`·`2026-038` 오답 0.4762 | `410852b07001` **1.0000 / 1위**, 2위 0.4762 |
| `프로젝트 투입 관리_2026-031_한국자산관리공사` | 상위 10위 안에 **아예 없음**, 1위는 `2026-032_동양생명` 0.6207 | `410852b07001` **1.0000 / 1위**, 2위 0.6207 |
| 위 질의의 NFD 표기 | (측정 안 함 — 저장·질의 둘 다 NFD라 우연히 맞을 수도) | `410852b07001` **1.0000 / 1위** |

**회귀 테스트.** `tests/storage/test_document_title.py`(순수 함수: NFD Drive 이름/Gmail 제목/헤딩이
NFC로 저장되는가, 300자 자르기가 정규화 **뒤에** 오는가 — 순서가 바뀌면 NFD 3코드포인트가 NFC
1글자로 합쳐지면서 자르는 위치가 달라진다), `tests/storage/test_lexical_store.py`(DB: NFD 제목↔NFC
질의, NFC 제목↔NFD 질의, 청크 양방향, `normalize_unicode_text`의 수정 건수·멱등성). DB 쪽은
`KF_TEST_DATABASE_URL`이 있어야 돈다.

**남겨둔 것.** `record_chunk_embeddings`는 저장 직전의 **메모리상 `Chunk` 객체**를 임베딩하므로,
NFD로 파싱된 청크는 NFC로 저장되면서도 임베딩은 NFD 텍스트로 만들어진다. 토크나이저가 다르게
쪼개니 벡터 품질에 미세한 영향이 있을 수 있지만, 이번에 깨진 건 어휘 arm이고 임베딩 쪽은 측정한
바가 없어 범위 밖으로 뒀다. 고친다면 `Chunk` 모델이나 청커 출력이 정규화 지점이 된다.

### 어휘 검색 SQL이 trigram 인덱스를 타게 (2026-09-14, `perf/lexical-sql`)

**문제.** `search_chunks_lexical`의 `WHERE (text ILIKE p OR text ILIKE ALL(tokens) OR word_similarity(q, text) >= 0.5)`는
`word_similarity()`가 인덱스 연산자가 아니고 `ILIKE ALL(array)`도 GIN이 받지 못해 항상 `Seq Scan on chunks`였다 -
행마다 긴 본문의 trigram을 만들어 유사도를 계산하는 게 비용의 정체다(2,856청크에서 호출당 ~350-460ms). term
fallback이 질의당 최대 10회 부르므로 질의 지연의 대부분이 여기서 났다.

**선택한 형태.** 결과는 그대로 두고 후보 수집만 바꿨다(`postgres_store.py::_lexical_candidates_sql`).

```sql
-- READ ONLY + 무조건 롤백 트랜잭션 안에서 (_read_transaction)
SET LOCAL pg_trgm.word_similarity_threshold = 0.5;  -- 제목은 0.3
SET LOCAL enable_seqscan = off;
WITH hits AS MATERIALIZED (
  SELECT chunk_id FROM chunks WHERE text ILIKE %(pattern)s
  UNION SELECT chunk_id FROM chunks WHERE %(query)s <% text
  UNION SELECT chunk_id FROM chunks WHERE text ILIKE %(token0)s AND text ILIKE %(token1)s ...  -- 토큰이 전체 패턴과 다를 때만
)
SELECT ... is_substring, word_similarity(q, c.text) AS sim  -- 후보에만 계산
FROM hits JOIN chunks c ... ORDER BY/LIMIT 기존과 동일
```

- **결과 동일성 근거.** 세 분기의 합집합이 예전 OR 술어와 같은 집합이다. `q <% col`은 pg_trgm 내부에서
  `word_similarity(q, col) >= pg_trgm.word_similarity_threshold`와 같은 계산(임계값을 넘는 순간 멈추는 최적화만 있음)이고,
  GIN의 `<%` consistent는 "질의 trigram 중 행에 있는 비율 >= 임계값"이라는 상한이라 재검사로 정확해진다.
  `ILIKE ALL(array)`를 `ILIKE ... AND`로 펼친 것은 같은 술어다. 정렬 키(`document_id, sequence_index` / `title, document_id`)는
  전순서라 계획이 바뀌어도 순서가 흔들리지 않는다.
- **all-tokens tier를 다른 분기에서 유도하지 않은 이유.** 2글자 한글 토큰("명함 제작")은 LIKE 패턴에서 trigram이 0개라
  그 tier의 행이 substring/`<%` 분기에 들어 있다는 보장이 없다. 그래서 별도 분기로 두고, trigram 없는 패턴은
  GIN 전체 스캔 + 재검사로 정확히(느리지만) 답하게 했다. 공백 없는 질의(term fallback의 대부분)는 토큰 분기가
  substring 분기와 같아 생략한다.
- **OR 하나(BitmapOr)가 아니라 UNION인 이유.** BitmapOr는 재검사 때 OR 전체를 평가한다. 한 분기가 trigram 없는
  패턴(2글자 term)이면 비트맵이 전체 행이 되고, 그 모든 행에 `word_similarity`를 계산하는 예전 비용으로 돌아간다
  ("AI 보고" 실측 464ms).
- **`SET LOCAL enable_seqscan = off`를 쓴 이유.** 분기를 나눠도 이 규모에서는 플래너가 `<%` 분기를 순차 스캔으로
  골랐다(7어절 질의 361ms). 함수 비용 추정(`COST 1`)이 긴 본문의 trigram 생성 비용을 모르기 때문이다. 플래너 설정은
  계획만 바꾸고 결과는 바꾸지 못하며, 이 트랜잭션에는 이 SELECT 하나만 있다. MATERIALIZED CTE만으로는 분기 안의
  스캔 선택을 강제하지 못해 대안이 되지 않았다.
- 제목 검색(`search_documents_by_title`)도 같은 빌더를 쓴다. 문서 358행이라 절대 이득은 작다.

**동일성 증거(`knowledge_forge`, 읽기 전용).** 예전 SQL을 그대로 복사한 구현과 새 구현을 같은 DB에서 나란히 불러
결과 리스트(dict 전체 - id·순서·score·snippet)를 비교했다. 문자열 2,508개 = 두 골든셋 질의 562개 + 그 공백 토큰 1,540개 +
`keyword_terms` term 1,103개 + 경계 사례 14개(`""`, `"?"`, `"_"`, `"%"`, `"\\"`, `"AI"`, `"명함"`, 9토큰 질의 등).
- 청크: limit 10/20/60/200 전부(새 결과를 예전 limit 200의 앞부분과 비교 - 정렬이 전순서라 유효), 골든 질의는 예전
  limit 10/20/60을 직접 호출해서도 비교.
- 제목: limit 10/20/60/200 전부 직접 비교(재채점 때문에 앞부분 비교가 성립하지 않는다).
- 필터 변형(골든 질의, limit 20): `project_id`, `since`, `until`, 셋 모두, 제목은 `source_type`+`since`까지.
- **26,808회 비교(279,294행), 불일치 0.**

**지연(순차, 쌍마다 old/new 순서 무작위, 골든 562 + term 300, 다른 평가 작업과 DB 공유 중).**

| 호출 | old p50 / p95 | new p50 / p95 | 쌍별 new/old p5 / p50 / p95 | 느려진 쌍 |
|---|---|---|---|---|
| `search_chunks_lexical` limit 20 | 372.0 / 497.7ms | 9.4 / 100.1ms | 0.014 / 0.022 / 0.281 | 1/862 ("to" 348→450ms) |
| `search_documents_by_title` limit 10 | 6.9 / 9.0ms | 4.1 / 6.5ms | 0.480 / 0.584 / 0.892 | 12/862 (최대 1.42배, 절대 1ms 안팎) |

EXPLAIN ANALYZE(새 SQL): 7어절 질의는 세 분기 모두 `Bitmap Index Scan on idx_chunks_text_trgm`, 2.5ms(예전 380ms).
"한화손해보험" 67ms(예전 357ms) - 후보 54행의 `<%` 재검사와 SELECT의 `word_similarity`가 대부분이다. "명함"(trigram 없는
2글자 패턴)은 substring 분기가 GIN 전체 스캔 + 재검사 2,853행으로 65ms. "AI 보고" 304ms(예전 464ms) - fuzzy 후보 346행.

**남은 비용과 하지 않은 것.** 느린 꼬리는 fuzzy 후보가 수백 행인 짧은 term("to", "AI", "70")이다. 후보마다 유사도를
두 번(인덱스 재검사 + 정렬용 SELECT) 계산하고, trigram 없는 패턴은 전체 인덱스 스캔이 된다. 스키마를 바꾸는 개선은
하지 않고 기록만 한다: `word_similarity`/`word_similarity_op`의 `COST`를 올리면 `enable_seqscan` 없이도 플래너가
인덱스를 고른다(확장 소유 함수라 `ALTER FUNCTION`은 DDL). 1-2글자 토큰을 인덱스로 받으려면 bigram 인덱스
(`pg_bigm`) 같은 별도 확장이 필요하다.

### 벡터 경로(pgvector) — 임베딩이 실제로 채워졌을 때 (2026-09-04 검증)

`knowledge_object_embeddings`가 개발 DB에서 0행이라, `vector_search` → `search_by_embedding`
→ `retrieve()`의 RRF까지 이어지는 경로는 **실데이터로 한 번도 실행된 적이 없었다**(골든셋 76개
질의 전부에서 `vector_search` 0히트 - 버그가 아니라 데이터 부재). 임베딩을 채우기 전에 미리
검증한 결과를 여기 남긴다. 재현 테스트는 `tests/search/test_vector_path.py`(결정론적
`ConceptEmbedder`로 API 키 없이 돈다)와 `tests/storage/test_embedding_diagnostics.py`.

**확인된 것** (모두 테스트로 고정):
- 청크 grain 저장(`record_chunk_embeddings`) → `vector_search`가 **표면형이 전혀 겹치지 않는**
  질의로 그 청크를 1위로 찾아낸다. `pgvector.Vector` 바인딩(INSERT/조회 양쪽), 코사인 거리
  → 유사도(`1 - distance`) 부호, `ORDER BY`/`LIMIT`이 실제로 맞다.
- `target_kind`(`chunk` / `knowledge_object` / 생략=혼합), `project_id`, `embedding_model`,
  `since`/`until` 필터가 전부 벡터 경로에서도 실제로 걸린다. 특히 다른 모델 이름으로 저장된
  같은 청크의 벡터는 랭킹에 절대 섞이지 않는다.
- 청크 히트의 `summary`는 `embedded_text`를 500자(`_CHUNK_HIT_SUMMARY_CHARS`)로 자른 것,
  KO 히트의 `summary`는 KO의 summary - 한 키로 통일된다.
- `retrieve()`의 RRF(`merge_and_rerank`)에서 벡터 히트가 최종 랭킹에 실제로 반영된다. 소스
  이름은 `vector_search`로 일치하고, 어휘 검색이 전혀 못 잡는 문서가 벡터 단독으로 결과에 남는다.

**고친 버그: 필터가 걸린 ANN이 히트를 통째로 잃었다** (`PostgresStore._configure_ann_search`)

HNSW 인덱스 스캔은 거리 순으로 그래프를 걸으며 대략 `hnsw.ef_search`(기본 40)개를 실행기에
넘기고, WHERE는 **그 뒤에** 적용된다. 그래서 필터가 테이블의 소수만 고르면 후보가 전부 필터에
지워지고 스캔은 "더 없음"을 반환한다 - 조건에 맞는 행이 분명히 있는데도 0히트. 이 스키마에서
실측: 한 프로젝트에 4000행 + 다른 프로젝트에 3행을 넣고 작은 쪽으로 `search_by_embedding`을
부르면 기본값(`hnsw.iterative_scan = off`)에서 **0행**, `strict_order`에서 정상적으로 3행.
`EXPLAIN`상 `Index Scan using idx_koe_embedding_hnsw ... Rows Removed by Filter: 20`.

가설이 아니라 상시 경로다: 실행기는 **항상** `project_id`로 거르고, temporal 질의는 거기에
`since`/`until`을 더한다. 그래서 `connect()`(및 `init_schema()`)에서 세션 GUC
`hnsw.iterative_scan = strict_order`를 켠다. `relaxed_order`가 아닌 이유는 `search_by_embedding`이
거리를 그대로 `score`로 바꿔 호출자가 그 순서를 신뢰하기 때문. 작업량은 pgvector 자신의
`hnsw.max_scan_tuples`(기본 20000)로 묶이고, 필터가 없는 질의의 지연시간은 실측상 차이가 없다.
GUC가 없는 pgvector(<0.8)에서는 경고만 남기고 넘어간다.

**고치지 않았지만 반드시 알고 있어야 하는 것 두 가지** — 둘 다 "임베딩 0행" 시절의 골든셋
점수(v0 ndcg@10 0.865)를 임베딩을 채운 뒤와 비교할 때 지표를 움직인다. 임계값을 찍어 맞히는
대신 골든셋 IR 평가로 재고 나서 결정할 문제인데, 그 평가 하네스는 2026-09-14 삭제되어
재구현이 먼저다 - 지금은 동작만 테스트로 고정해 두었다:

1. **키워드 fallback이 사실상 영원히 안 돈다.** `_Run._keyword_fallback`/`_term_fallback`은
   벡터가 0히트일 때만 도는 degrade 경로인데(임베딩 0행 상태를 위한 것), ANN은 관련도와 무관하게
   항상 `limit`개를 돌려주므로 임베딩이 채워지는 순간 조건이 성립하지 않는다. term 분해 fallback은
   v0에서 ndcg@10을 0.709 → 0.865로 올린 바로 그 경로다.
2. **ANN에는 관련도 하한이 없다.** 코퍼스에 전혀 없는 주제를 물어도 유사도 0인 문서들이 후보로
   올라온다. `exhaustive`/`count`/`negative`는 top-k 컷오프 없이 이 후보 집합을 그대로 답(문서
   집합/개수)으로 쓰므로, `predicted_count`가 부풀고 "관련 문서 없음"이 정답인 질의가 무언가를
   돌려주게 된다(`fetch_limit`이 `EXHAUSTIVE_LIMIT`=200인 것도 여기서 곱해진다).

**`suggest_strategies`의 짧은 semantic_query** (확인만, 미수정): `semantic_query`가 비어 있지
않기만 하면 `vector`가 붙는다. v0/v1-draft 골든셋 94개 질의에 규칙 추출을 돌려 보면 실제로
"보낸"(`nonghyup.com 도메인 발신자가 보낸 메일`), "내용"(`… .pptx 내용을 요약해줘`)처럼 의미가
남지 않은 잔여 토큰으로 벡터가 붙는 경우가 있다(6자 이하 7건). 다만 **길이 기준으로 막으면 안
된다** - 같은 구간에 "동양생명"/"광주은행"/"인수인계"처럼 정상적인 4자 개념어가 섞여 있다.
올바른 수정은 `_FILLER_RE`에 잔여 기능어를 더하는 쪽이고, 그건 골든셋 IR 회귀로 재고 나서
할 일 - 그 평가 하네스는 2026-09-14 삭제되어 재구현이 먼저다.

**실적재 확인** (2026-09-04, 청크 936개): 실제로 저장된 `embedding_model`은
`openai/text-embedding-3-small`(OpenRouter의 모델 id)이고 `Settings.embedding_model`의 기본값은
`text-embedding-3-small`이다 - 즉 **`.env`가 로드되지 않는 CWD에서 검색을 돌리면 그것만으로
0히트가 된다**(pydantic의 `env_file=".env"`는 CWD 상대경로라 git worktree 안에서는 안 잡힌다).
실데이터로 `search_by_embedding`을 돌려 히트 모양/`summary` 길이(496자)/`content_at`/self-match
score 1.0까지 확인했다. 936행에서는 플래너가 아직 정확 Sort를 골라 위의 ANN 필터 문제가
발동하지 않는다 - 비용 기반이라 행이 몇 천 개로 늘면 HNSW 경로로 넘어간다(그때가 바로 위
`_configure_ann_search`가 막는 지점이다).

**진단**: `PostgresStore.embedding_coverage(embedding_model, project_id)`(→ `PipelineStore`에도
노출). 벡터 검색이 조용히 0히트가 되는 원인 세 가지 - 행이 없다 / 저장할 때와 검색할 때 모델
이름이 다르다 / 다른 프로젝트에만 있다 - 를 한 번에 보여준다(`rows_by_model_and_kind`는 모델
필터 없이 집계하므로 이름 불일치가 바로 드러난다). CLI 노출은 아직 없다(`cli.py`는 이 작업
범위 밖) - `knowledge-forge db embedding-coverage` 정도로 얇게 감싸면 된다.

## ④ Context Engineering — 부분 구현

`agent/compaction.py`가 **토큰 예산** 관점의 압축(오래된 turn을 요약으로 치환)은 이미 하고
있고, evidence 단위에서는 `search/rerank.py`의 **cross-source dedup**(같은 `knowledge_object_id`
를 가진 hit을 하나로 합침, 위 ③ 참고)까지는 구현되어 있다. 하지만 RAG 맥락에서 말하는 나머지
context engineering(conflicting information 탐지, source reliability 평가, temporal
consistency, relevance filtering, evidence clustering)은 없다. 지금 규모(`plan_search` 하위
질의당 최대 5건, 소스 최대 2개)에서는 모델 컨텍스트 윈도우에 그대로 넣어도 감당 가능하다고 보고
미룬 것 - evidence 개수/소스 수가 늘어나면 가장 먼저 필요해질 레이어.

## ⑤ Map-Reduce / Hierarchical Retrieval — 부분 구현 (document expansion만, `search/document.py`)

"지난 1년간 회사 AI 전략 변화 요약" 같은, 문서 수천~수만 개를 partition→map→reduce하거나
Message→Chunk 요약→Document 요약→Project 요약처럼 계층적으로 사전 요약해 둬야 하는 질문은 지금
스코프 밖이다. `pipeline.py`가 이미 문서 단위 map-reduce 요약(`extraction/schemas.py`의
`ExtractionResult.summary`를 모아 재요약)을 고민한 흔적이 `roadmap.md`(7단계 이전 문서 요약
설계)에 있으니, 필요해지면 그 설계를 확장하는 방향이 될 것.

### Document Expansion (`read_document` 도구, `search/document.py`) — **구현됨**

위 문단의 "수천 문서 map→reduce"는 여전히 스코프 밖이지만, 그 계층의 가장 작은 단위 - **문서
하나를 통째로, 순서대로 읽는 것** - 은 top-k 검색만으로는 아예 답할 수 없는 질의 유형이 실제
질문에서 반복돼서 먼저 구현했다. 청크 grain 임베딩(`embeddings.md`)이 들어오면서 `vector_search`가
`document_id`를 히트마다 싣게 됐으니, "검색으로 문서를 특정 → 그 문서를 확장(expand)해서 읽는다"
는 2단계가 자연스럽게 열린 것이기도 하다.

**왜 필요했는가 - top-k로 못 푸는 네 가지 질의 유형**:

| 유형 | 예 | top-k가 실패하는 이유 |
|---|---|---|
| whole-document summary | "2025_SH_업무_보고서 요약해줘" | 요약은 문서 전체가 입력이어야 한다 - 상위 k청크는 "잘 매칭된 일부"일 뿐 |
| long-range extraction | "보고서 전체에서 핵심 리스크만 정리" | 리스크는 문서 곳곳에 흩어져 있고 질의 임베딩과 개별 청크의 유사도는 낮다 |
| sequence / ordering | "회의에서 처음 제안된 내용과 최종 결정 비교" | "처음"/"최종"은 문서 내 **순서** 정보인데 랭킹은 순서를 버린다 |
| negative / absence | "A 프로젝트에서 언급되지 않은 기술은?" | 부재는 전체를 다 봐야 증명된다 - 검색 결과에 없다는 것은 문서에 없다는 뜻이 아니다 |

**무엇을 만들었나** (계층 설계는 `agent-runtime.md`의 원칙 그대로 - 서비스 함수 하나, 얇은 어댑터 둘):

- `PostgresStore.list_chunks_by_document(document_id, *, hierarchy_level=None)` (+`PipelineStore` 위임):
  `sequence_index` 순 정렬. **기본이 "모든 hierarchy_level"인 이유**: `chunking/adaptive.py`는 leaf
  단위만 Chunk로 물질화한다 - 분할된 상위 단위는 emit되지 않고 `parent_chunk_id`는 항상 None,
  `(document_id, sequence_index)`는 유일하다. 즉 한 문서의 청크는 레벨이 섞여 있어도(개발 DB 263건
  중 2건: paragraph+sentence 이메일 스레드, chapter+section 보고서) 겹치지 않는 하나의 시퀀스라,
  특정 레벨만 고르면 본문이 빠진다. `hierarchy_level` 필터는 나중에 multi-resolution 저장소가 생겼을
  때를 위한 것이고 `read_document`는 쓰지 않는다.
- `search/document.py::read_document(document_id, store, *, max_chars=None, offset=0)`: `{document_id,
  title, source{source_id, source_type, uri, modified_at}, total_chunks, total_tokens, offset, chunks[{chunk_id,
  sequence_index, page_start, page_end, text}], truncated, next_offset}`. 없는 문서는 None. `title`은
  `documents.metadata.title`만 본다(별도 컬럼 가정 없음). `store`는 Protocol이라 `PipelineStore`(도구)와
  `PostgresStore`(HTTP 라우트) 둘 다 받는다.
- agent 도구 `read_document` (`agent/tools.py::_make_read_document_tool`): `vector_search`와 같은
  "`KF_DATABASE_URL` 있을 때만 등록, 호출마다 `PipelineStore` 열기" 패턴. 없는 ID는 `read_file`의
  FileNotFoundError처럼 예외 → `agent/loop.py`가 "error: document not found: …" tool_result로 바꾼다.
- `GET /documents/{document_id}/chunks?offset=&max_chars=` (`api/routes/sources.py`): 같은 응답 모양
  (`DocumentChunksResponse`) - UI가 긴 문서를 넘겨볼 때 에이전트와 같은 `next_offset` 계약을 쓴다.

**컨텍스트 예산 결정** - 검색 히트에 `_CHUNK_HIT_SUMMARY_CHARS`(500자)/`_MAX_SEARCH_LIMIT`(50)을
두는 것과 같은 걱정을 문서 단위로 옮긴 것:

- 페이지 단위는 **글자 수 예산(`max_chars`) + 청크 인덱스 오프셋(`offset`)**. 청크를 중간에서 자르지
  않는다(자르면 그 뒷부분을 다시 읽을 방법이 없다). 예산이 차면 `truncated: true`와 다음 청크 인덱스
  `next_offset`을 돌려주고, 시스템 프롬프트가 "끝까지 이어 읽으라"고 지시한다 - 부재 확인 질의는
  마지막 페이지까지 읽어야 답이 성립하기 때문.
- **페이지의 첫 청크는 예산을 넘어도 항상 포함**한다 - 청크 하나가 `max_chars`보다 길 때 빈 페이지 +
  같은 `next_offset`을 돌려주면 호출자가 영원히 제자리걸음이다. 청크 상한은 chunker의 토큰 예산(기본
  4000×0.8)이라 초과 폭은 유한하다.
- 도구 기본 `max_chars`는 **20,000자**, 서버 상한 **50,000자**. 근거는 개발 DB 실측(문서 263건/청크
  936건 기준 문서당 평균 ~770자, 최장 ~21.6K자/437청크): 기본값이면 거의 모든 문서를 한 번에 읽고
  최장 문서도 두 페이지면 끝난다. 한국어 20K자는 대략 15~25K 토큰 - 컨텍스트 윈도우의 한 자리 수 %
  - 이고, 상한은 모델이 큰 값을 불러도 tool_result 하나가 예산을 통째로 날리지 못하게 막는다
  (`_clamp_limit`, "모델이 부른 값을 그대로 믿지 않는다"). `total_tokens`(청크 `token_count` 합)를 같이
  실어서 모델이 "몇 페이지짜리 문서인지"를 첫 호출에서 가늠하게 했다.
- 매 페이지 청크 전체를 읽고 서비스에서 자른다 - 현재 규모에서 한 번의 조회가 충분히 싸고,
  페이지마다 총계를 다시 세는 별도 쿼리보다 단순하다. 문서가 수천 청크로 커지면 store에 LIMIT/OFFSET을
  내리고 총계 쿼리를 분리하는 게 다음 단계.

**여전히 안 한 것**: 문서 여러 개를 한 질문에 확장하는 map-reduce(문서 N개 × 20K자는 예산 안에 안 든다 -
그때는 `delegate_task`로 문서당 서브에이전트를 돌려 요약을 모으는 것이 지금 도구로 가능한 우회),
문서 요약의 사전 계산/캐시(`roadmap.md`의 문서 요약 설계), 청크 텍스트 안의 표/이미지 전사 품질.

## ⑥ Temporal Search — 구현됨, `search/temporal.py` + `search/sql.py`

"어제", "지난주", "이번 분기", "2025년 Q3", "최근 3일" 같은 상대/분기 시간 표현을
`parse_temporal_expression(text, now=None) -> TimeRange | None`이 명시적 `[start, end)` UTC
범위로 바꾸고, `file_search`/`vector_search`가 그 범위로 결과를 필터링한다.

- **LLM 호출이 아니라 정규식 + 달력 산술**이다 - `search/router.py`와 같은 이유: "어제"를 explicit
  range로 바꾸는 건 `now`가 고정되면 완전히 기계적인 계산이라, 여기에 LLM 라운드트립을 쓰면
  정확도 개선 없이 지연/비용만 늘어난다.
- **필터 적용 지점**: **`KnowledgeObject.extracted_at`(파이프라인이 그 KO를 처리한 시각) 기준이
  아니다** - 처음엔 그렇게 구현했다가 실측 중 실제로 잘못된 결과(아래 "실측으로 발견한 버그"
  참고)를 내는 걸 확인하고 **소스의 실제 시각**으로 바꿨다. `file_search`는 frontmatter의
  `content_at`(오브젝트) 또는 `modified_at`(리소스) - 둘 다 없는 옛날 파일만 `generated.at`으로
  폴백(`search/service.py::_content_at`) - 를, `vector_search`는
  `COALESCE(sources.modified_at, sources.collected_at)`을 기준으로 `[since, until)` 필터를
  건다. `okf/writer.py::write_objects()`가 오브젝트를 쓸 때 그 오브젝트가 속한 리소스의
  `modified_at`(없으면 `collected_at`)을 찾아 `content_at`으로 같이 찍어준다.
- **실측으로 발견한 버그**: 스키마 설명만 보고 SQL을 직접 짜게 한 블라인드 테스트에서, 에이전트가
  처음엔 (당시 구현대로) `extracted_at`으로 "지난달"을 필터링했다가 0건을 받고, "이건 파이프라인
  처리 시각이지 실제 메시지 시각이 아니다"라고 스스로 진단해 `sources.modified_at`으로 바꿔
  재시도한 뒤에야 정확한 결과를 얻었다 - 실제 데이터가 몇 주치 Gmail/Slack 이력을 한 번에
  배치로 추출한 것이라 `extracted_at`이 전부 그 배치를 돌린 하루로 뭉쳐 있었기 때문. 이 발견을
  반영해 `file_search`/`vector_search` 둘 다 소스의 실제 시각 기준으로 고쳤다 - `modified_at`이
  없는 소스는 `collected_at`(그래도 "우리가 언제 수집했는지"라 "실제로 언제 있었던 일인지"의
  근사치이긴 하지만 `extracted_at`보다는 훨씬 낫다)으로 폴백한다.
- **어디서 자동으로 쓰이는가**: (1) `plan_search`(`search/planner.py::_execute_subquery`)가 각
  하위 질의 문장 자체를 스캔해 시간 표현을 찾으면 자동으로 그 하위 질의의 검색에 적용한다 -
  모델이 별도 인자를 신경 쓸 필요가 없다. (2) `file_search`/`vector_search` 도구 자체도
  선택적 `time_range`(자연어 문자열) 인자를 받는다 - 모델이 직접 이 두 도구를 호출할 때도
  "어제" 같은 표현을 그대로 넘기면 되고, 날짜 계산은 여기(`search/temporal.py`)가 대신한다.
  인식하지 못하는 표현은 에러 없이 그냥 무시된다(필터 없이 검색).
- **타임존**: 항상 UTC 기준(이 시스템의 모든 타임스탬프-`extracted_at`/`collected_at`/
  `modified_at` 전부-가 UTC로 저장되므로 데이터와 일관성을 맞춘 것) - 호출자가 UTC와 먼
  타임존에 있으면 "오늘"/"어제"의 자정 경계가 그 호출자의 체감과 최대 하루 정도 어긋날 수
  있다. 월요일을 주의 시작으로 본다(ISO, 한국 관례와 일치).

### Text-to-SQL (`sql_search` 도구, `search/sql.py`) — Temporal Join과 속성/entity 필터의 실제 해법

"지난달 유지희가 진행한 업무 요약"처럼 **시간 범위 + 특정 사람(속성) 필터를 동시에** 거는
질문은 `since`/`until` 파라미터 하나로는 풀리지 않는다 - "유지희" 쪽은 애초에 구조화된 필터
파라미터가 없기 때문(위 ① Query Understanding, ③ Hybrid Retrieval의 "entity 필터 미구현"
gap). 이걸 속성 하나마다 새 파라미터를 만드는 대신, **모델이 직접 SQL을 작성**해서 풀도록
`sql_search` 도구를 추가했다:

```sql
-- "유지희"라는 entity를 언급한 항목 중 지난달 것만
SELECT ko.summary, ko.extracted_at
FROM knowledge_objects ko
JOIN relations r ON r.subject_id = ko.knowledge_object_id AND r.type = 'mentions'
JOIN knowledge_objects e ON e.knowledge_object_id = r.object_id AND e.type = 'entity'
WHERE e.content->>'name' = '유지희'
  AND ko.extracted_at >= '2026-08-01' AND ko.extracted_at < '2026-09-01'
```

- **왜 별도 자연어→SQL 변환 LLM 호출이 없는가**: `sql_search`를 호출하는 모델 자신이 이미 그
  질문을 낸 LLM이다 - `agent/tools.py`가 도구 설명에 스키마(`search/sql.py::SCHEMA_DESCRIPTION`)
  를 실어 보내는 것만으로, 모델이 도구 인자로 SQL을 바로 써서 넘긴다. `search/planner.py`의
  `decompose_query()`처럼 별도 LLM 라운드트립을 도는 게 아니다 - 이미 하고 있는 추론을 다시
  하게 만들 이유가 없다.
- **읽기 전용 강제, 세 겹**: (1) `search/sql.py::_validate_readonly_sql`이 SELECT/WITH로
  시작하는 단일 문인지, 쓰기/DDL 키워드가 없는지 문자열 단계에서 먼저 걸러낸다. (2)
  `PostgresStore.execute_readonly_query`가 그 SQL을 `SET TRANSACTION READ ONLY` 트랜잭션
  안에서 실행한다 - (1)이 놓친 게 있어도 Postgres 자체가 쓰기를 거부한다. (3) 그 트랜잭션은
  `force_rollback=True`(psycopg3)로 성공하더라도 무조건 롤백된다. 셋 다 독립적인 방어선 -
  잘 작동하는 모델이면 (1)만으로 충분하지만, 검색된 문서 내용을 통한 prompt injection처럼
  모델이 나쁜 SQL을 쓰도록 유도당하는 경우까지 감안한 설계.
- **행 수 제한**: 모델이 쓴 SQL의 `LIMIT`을 신뢰하지 않는다 - 전체 쿼리를 서브쿼리로 감싸
  `SELECT * FROM (모델의 SQL) AS sql_search LIMIT 200`(기본값)을 서버가 강제한다.
- **JSON 직렬화**: `agent/loop.py`의 tool_result 직렬화(`json.dumps`, `default=` 없음)가 임의
  컬럼 타입(datetime/Decimal/UUID/bytes)을 만나면 그냥 죽는다 - `search/sql.py::_json_safe`가
  모든 값을 미리 JSON-안전 타입으로 바꾼다. 1536차원 벡터처럼 긴 숫자 리스트는
  `"<vector, N dims, omitted>"`로 치환한다(스키마 설명에도 embedding 컬럼을 SELECT하지 말라고
  적어뒀지만, 실수로 걸리는 경우의 안전망).
- **project_id 스코프는 강제되지 않는다** - 이 도구는 다른 도구들과 달리 `project_id` 파라미터가
  없다(임의 SQL이라 외부에서 안전하게 주입할 방법이 마땅치 않다). 시스템 프롬프트가 "필요하면
  SQL 안에 직접 sources와 JOIN해 WHERE로 걸 것"이라고 안내할 뿐이다 - 이 harness 전체가 이미
  전제하는 "접근 제어 없음, 로컬/단일 사용자 실행"(agent-runtime.md의 "알려진 제약")과 같은
  신뢰 경계.
- **Graph 축도 부분적으로 대체**: `relations` 테이블을 직접 JOIN할 수 있으므로, 별도
  `graph_search`(`GET /relations/expand`, 여전히 미구현) 없이도 "무엇이 무엇을 언급/인과하는지"
  같은 질문의 상당 부분을 `sql_search`로 풀 수 있다 - 다만 `GET /relations/expand`가 설계해
  둔 `depth>1` 재귀 탐색까지 모델이 매번 정확한 재귀 CTE를 직접 쓰길 기대하긴 어려워서, 얕은
  1-hop JOIN 수준의 대체다.

## Evidence Grounding / Citation — 약하게 구현

`agent/prompts.py`가 "근거로 삼은 검색 결과의 path/project_id(또는 knowledge_object_id)를
인용할 것"을 지시하지만, 이건 프롬프트 수준의 요청이지 강제되는 파이프라인이 아니다 - 답변에서
claim을 추출해 evidence와 자동으로 매칭/인용하는 별도 단계(Claim extraction → Evidence matching
→ Citation)는 없다. 모델이 지시를 무시하면 인용 없는 답이 나올 수 있다.

## Search Evaluation / Feedback Loop — 재구현됨 (2026-09-14)

옛 구현(로깅·피드백·LLM-judge·`eval-ir`/`eval-gen`)은 2026-09-14 전면 삭제했고 같은 날 작게 다시 만들었다.
`knowledge-forge eval gen|run|compare` 세 명령, 모듈 4개(`src/knowledge_forge/eval/`), DB 테이블 없음.

- 골든셋은 생성할 때 grain(`chunk` 또는 `knowledge_object`)을 정하고, 평가는 그 grain으로만 검색·채점한다
  (`retrieve(grain=)`가 모든 벡터 조회를 그 임베딩으로 고정).
- 기본은 retrieval 지표(LLM 비용 0), `--e2e`는 에이전트 루프 + 답변 judge.
- 사람 피드백 로깅과 온라인 groundedness gate는 재구현 범위에 없다.

설계·게이트·지표 정의는 [`evaluation.md`](evaluation.md).

## 왜 지금 여기서 멈추는가

`agent-search.md`의 vector_search 구현에 이어, 이번에는 **Query Planning + Query Routing**을
우선순위로 골라 구현했다(`search/planner.py`/`search/router.py`, `plan_search` 도구) - 이유는
두 가지: (1) 검색 소스가 앞으로 2개(file_search/vector_search)에서 3~4개(graph/SQL 등)로
늘어날 걸 대비해 "질문마다 어떤 소스를 쓸지 결정하는 계층"을 미리 만들어 두는 게, 소스가 실제로
늘어난 뒤 각 소스 호출부마다 라우팅 로직을 흩뿌리는 것보다 싸다. (2) Iterative Retrieval은 이미
tool-calling 루프가 공짜로 제공하고 있었으니(위 참고), 남은 것 중 "지금 없으면 멀티홉 질문에서
바로 체감되는" 게 Planning이었다. 나머지 레이어(Hybrid rerank, Context Engineering, Map-Reduce,
Temporal Search, Evidence Grounding 파이프라인, Feedback Loop)는 각자 스코프가 크고, 지금
규모(검색 소스 2개, evidence 수 적음)에서는 당장 병목이 아니라고 판단해 설계만 남긴다 - 필요한
순서는 대략 위에 정리한 순서(③ hybrid rerank → ④ context engineering → ⑥ temporal → ⑤
map-reduce → citation → feedback loop)가 될 것 같지만, 실제 우선순위는 어떤 게 먼저 병목이
되는지 보고 정할 문제다.

바로 이어서, 그 순서의 첫 항목인 **③ Multi-stage Retrieval/Reranking**(RRF, `search/rerank.py`)
과 거기서 자연스럽게 같이 풀리는 **④ cross-source dedup**을 구현했다 - `plan_search`가 하위
질의당 두 소스를 병행 실행하기 시작한 순간부터 "결과를 그냥 이어붙이기만 한다"는 게 가장 먼저
체감되는 품질 문제였기 때문(같은 KnowledgeObject가 두 번 나오거나, 두 소스 다 낮게 평가한
결과와 둘 다 동의한 결과가 구분 없이 뒤섞임).

이어서 **⑥ Temporal Search**(`search/temporal.py`)를 구현했다 - "어제 뭐 있었어" 같은 질문이
지금까지 전혀 필터링되지 않고 그냥 키워드/의미 검색으로만 처리되는 게 가장 체감 격차가 큰
남은 gap이었다.

바로 뒤이어, "지난달 유지희가 진행한 업무 요약"처럼 시간 범위와 특정 사람/속성 필터를 **함께**
거는 질문(Temporal Join)에서 `since`/`until`만으로는 "유지희" 쪽이 전혀 안 걸린다는 게
분명해졌다 - entity 필터는 `project_id`/`type`처럼 전용 파라미터를 새로 만들어서는 대응이
안 되는 문제였다(속성이 무한히 다양할 수 있으므로). 그래서 **Text-to-SQL**(`search/sql.py`,
`sql_search` 도구)을 구현해 이 문제를 일반적으로 풀었다 - 속성 하나마다 파라미터를 추가하는
대신, 모델이 스키마를 보고 직접 SQL(WHERE/JOIN)을 쓰게 한 것. 이 결정 하나로 entity 필터
(③), temporal join(⑥), graph 1-hop 조회(③의 graph 축)까지 세 가지 이전 gap이 동시에
상당 부분 메워졌다 - 각각을 개별 기능으로 만드는 것보다, "구조화된 질의가 필요하면 SQL을
직접 쓰게 한다"는 하나의 결정이 더 일반적이고 쌌다.

남은 순서(⑤ map-reduce → citation → feedback loop, 그리고 ④의 나머지인 conflict 탐지/
reliability/clustering)는 여전히 설계만.

## 제목 매칭: 순서 의존 substring에서 토큰 집합으로 (2026-09-05)

### 문제

`storage/postgres_store.py::search_documents_by_title`의 1단계는 **질의 전체 substring**
(`title ILIKE '%q%'`)이고, 여기에만 score 1.0을 준다. 1.0은 그냥 높은 점수가 아니다 -
`search/retrieve.py::_claim_anchor`가 "이 질의와 사실상 같은 제목의 문서 하나"를 확정하는
신호이고, anchor가 잡히면 다른 전략의 히트까지 그 문서로 좁혀진다(`_narrow_to_anchor`).
즉 **제목이 정확히 맞았는지 아닌지가 그 질의의 검색 전체를 가른다.**

그런데 substring은 순서와 구분자에 묶여 있다. 토큰이 전부 들어 있어도 배열이 다르면 못 맞는다:

| 질의 | 실제 제목 | 예전 | 지금 |
|---|---|---|---|
| `Dev Handover README` | `README_ProjectIntel_Dev_Handover.md` | 0.825 (all-tokens tier) | **1.0** |
| `codex README` | `codex-README.md` | 1.0 (구분자 하나 차이라 운 좋게 맞음) | 1.0 |
| `인수인계 pptx` | `projectintel-development-handover.pptx` | 히트 없음 | 히트 없음(아래 "못 고친 것") |

사람이 파일명을 기억해서 칠 때 가장 먼저 날아가는 게 순서와 구분자다. 그래서 이건 질의가
이상한 게 아니라 매칭 규칙이 좁은 것이었다.

### 처방: 토큰 집합 일치 (`storage/title_match.py`)

구분자(`_` `-` `.` 공백)와 camelCase 경계, 한글/영문/숫자 경계로 잘라 소문자 집합으로 접고,
질의 집합과 제목 집합을 비교한다. 쪼갠 조각과 원래 덩어리를 **둘 다** 남기는 게 요령이다
(`ProjectIntel` → `projectintel`/`project`/`intel`) - 사람은 같은 이름을 붙여서도 띄어서도
쓰기 때문이다. NFC 정규화(`text_normalize.py`)는 이 단계 이전에 이미 끝나 있고, 그 위에 얹는다.

### 왜 anchor 임계값 인하가 아닌가 (실측으로 기각)

같은 증상의 다른 처방은 `_ANCHOR_MIN_SCORE`(1.0)를 낮추는 것이다. 그건 틀렸다.
`v0-ambiguous-002`(질의 `"README"`)는 제목 substring 1.0이 두 건(`codex-README.md`,
`README_ProjectIntel_Dev_Handover.md`)이고 **둘 다 정답**이라, 하나로 좁히는 순간 그 질의가
깨진다. 문제는 "1.0의 문턱이 높다"가 아니라 **"1.0의 판정식이 순서에 묶여 있다"**였다.
문턱을 낮추는 처방은 판정식을 그대로 둔 채 오답까지 통과시킨다.

### 가짜 1.0을 막는 세 가지 장치

새 tier가 1.0을 양산하면 검색이 통째로 망가지므로, 1.0은 두 경우에만 준다.
(1) 질의 토큰 집합 == 제목 토큰 집합, (2) 질의 토큰 집합 ⊂ 제목 토큰 집합이면서 **후보 중
그런 문서가 하나뿐**일 때. 그 외에는:

- **부분집합인데 유일하지 않으면 0.8**에 묶는다. 0.8인 이유가 설계의 핵심이다 -
  `_ANCHOR_MIN_GAP`이 0.2라, 토큰 tier가 주는 1.0 미만 점수의 상한을 0.8로 두면 1위가 1.0인
  질의에서 2위와의 격차가 항상 0.2 이상 유지된다. 즉 **이 tier는 (유일 판정을 통과했을 때)
  없던 anchor를 만들 수는 있어도, 있던 anchor를 깨뜨릴 수는 없다.**
- **점수를 내리지 않는다**(`max(기존 tier 점수, 토큰 점수)`). 내리면 반대로 anchor 격차가
  넓어져 없던 anchor가 생기는데, 그건 이 변경의 의도가 아니다.
- **정렬은 tier → 점수 순**이라, 토큰 tier가 아무것도 올리지 못한 질의의 결과는 이 tier가
  없던 때와 완전히 동일하다(공유 DB 페어드 비교에서 "움직인 질의가 이 변경 때문인지"를
  구분할 수 있어야 했다).

부분 일치(토큰 절반 이상이 겹치지만 부분집합은 아님)는 1.0 훨씬 아래의 별도 tier
(0.65~0.8)다. v0에서는 순위를 바꾼 질의가 하나도 없었다 - 측정된 이득도 손해도 없이,
"토큰이 반 이상 겹친 제목"을 퍼지 tier보다 위로 올려 두는 보험으로만 남아 있다.

### 못 고친 것

- **한글 질의 - 영문 제목**(`v0-navigational-003`: `"인수인계 pptx"` vs
  `projectintel-development-handover.pptx`). 공통 토큰이 확장자 `pptx` 하나뿐이라 토큰 집합
  으로는 원리적으로 안 된다. 문턱을 낮춰 억지로 맞히면 가짜 1.0이 쏟아진다. 번역/별칭
  사전(적재 시 제목의 영↔한 별칭 필드)이 있어야 하는 별개 문제로 남긴다. 같은 이유로
  `v0-ambiguous-003`(`"인수인계"`)는 제목 검색에서 히트가 **0건**이고, 정답은 전부 다른
  전략이 물어 온다.
- **`search_chunks_lexical`(본문)** 에는 같은 tier를 넣지 않았다. 제목은 짧아서 토큰 집합이
  곧 이름이지만, 청크 본문은 토큰이 수백 개라 "질의 토큰이 전부 들어 있다"가 흔해서 같은
  규칙이 변별력을 갖지 못한다.
- **유일성 판정은 가져온 후보 안에서만 참이다.** SQL이 `LIMIT`으로 자른 뒤 채점하면 판정이
  틀어지므로 `limit`의 4배(최대 200)를 받아 와서 재채점하고 자른다
  (`_TITLE_TOKEN_OVERFETCH`). 그래도 코퍼스가 훨씬 커지면 이 가정은 다시 봐야 한다.
---

## document_links를 랭킹에 쓰기 (`_Run.link_cohesion`)

`document_links`(백필 완료: `same_channel_sequence` 145, `thread_sequence` 58,
`previous_version` 1)는 지금까지 `retrieve()`가 **한 번도 읽지 않았다.** 읽게 만들면서
알게 된 것들.

### 먼저: 링크가 timestamp보다 더 아는 것은 "묶음" 하나뿐이다

두 sequence 링크는 "채널/메일함 안에서 시각상 인접한 두 건"이다. 그중 **순서**는 링크의
고유 정보가 아니다 - 모든 히트가 이미 `content_at`을 싣고 있고, 그쪽이 인접쌍이 아니라
전순서를 준다(`_Run.latest`가 쓰는 값이 그것이다). 그래서 "sequence/latest/temporal 유형이
낮은 건 링크를 안 써서"라는 가설은 **정렬 쪽으로는 틀렸다.** 링크가 추가로 말해 주는 것은
"이 두 문서가 같은 채널/스레드에 속한다"는 **묶음**뿐이고, 그건 타임스탬프만 봐서는 알 수 없다.
그래서 이 함수는 정렬이 아니라 응집도만 신호로 쓴다.

### 재현율 상한은 0이다 (링크로 새 문서를 데려오지 않는 이유)

v0 골든셋 76질의의 baseline 랭킹에 대해 "정답인데 랭킹에 없는 문서" 전부를 뽑아, 상위 10건에서
링크 한 홉으로 닿는지 실측했다 - **0건**이었다. `v0-latest-003`의 정답 `b2bf0036`,
`v0-multihop-003`의 `54e9f0ab`/`b2bf0036`은 `document_links` 행이 아예 없다. 즉 이 코퍼스에서
링크 확장으로 얻을 수 있는 재현율은 없고, 얻을 수 있는 것은 **순서**뿐이다. 그래서 확장은
후보 집합(`merged`) 안으로 제한한다 - 새 문서를 데려오면 `_Run.latest`가 예전에 밟은 붕괴
("관련도와 무관한 문서를 위로 올리면 후보가 늘수록 나빠진다")를 되풀이할 뿐 얻을 것이 없다.

### `thread_sequence`는 대화가 아니다 (링크 이름을 믿으면 안 된다)

`context/gmail.py::GmailThreadReconstructor`는 **메일함의 모든 스레드를 도착 시각 순으로
한 줄로** 잇는다(`ordered_threads`의 `previous_uri`). gmail 문서 하나가 이미 스레드 하나이므로,
이 간선이 말하는 것은 "이 스레드가 저 스레드 다음에 왔다"뿐이고 내용은 무관하다. 이름만 보고
"같은 대화"로 쓰면 서로 무관한 "공유 요청" 메일 두 건이 서로를 밀어 올린다 - 실측으로
`v0-factoid-001` 1.000 → 0.500, `v0-ambiguous-001` 1.000 → 0.571,
`v0-long_range_context-001` 1.000 → 0.500. 그래서 `_LINK_TYPES`에서 뺐다. 남긴 것은
`same_channel_sequence`(채널은 실제 묶음)와 오늘 0행인 `thread_parent`/`thread_reply`다.
`previous_version`은 `latest` 후처리가 이미 **반대 방향으로** 다루므로(구버전을 뒤로) 뺐다.

### 게이트: 질의가 "순서"를 물을 때만

링크 종류를 채널로 좁혀도, 게이트 없이 모든 질의에 응집도 표를 주면 채널 클러스터가 무관한
질의의 상위를 통째로 가져간다 - `v0-whole_document_summary-003`("AGENTS.md 전체 내용을
요약해줘")에서 슬랙 에피소드가 14위/62위에서 4위/9위로 올라와 정답을 밀어냈고(0.631 → 0.000),
`v0-factoid-003`에서는 같은 채널 6건이 몰려 올라와 1위 정답이 밀렸다(1.000 → 0.289). 표가
**문서 자신의 관련도 순위와 무관한 크기**(RRF 한 표는 62위든 2위든 같다)라서 생기는,
`_Run.latest` 붕괴와 같은 종류의 실패다.

그래서 `_SEQUENCE_INTENT_RE`(순서|순으로|차례|흐름|타임라인|시간순|순차)로 좁혔다. v0에서 이
게이트에 걸리는 질의는 4건뿐이다(sequence 3건 + `long_range_context-002`, 후자는 pptx라
링크가 없어 `no_cluster`로 끝난다).

**어휘 게이트를 여기서는 써도 되는 이유**(`_is_source_hint_only_filter`가 `source_type` 힌트를
못 믿는 것과 대비): `source_type` 힌트는 "정답이 **어디에** 있는지"에 대한 추측이라 틀리면
정답을 잘라낸다. "순서"는 "사용자가 **무엇을** 원하는지"이고, 오탐이어도 결과는 한 채널 안의
재정렬뿐이라 손실이 갇혀 있다.

### 표이지 고정이 아니다

anchor/comparison처럼 최상위로 끌어올리지 않고 RRF 한 표(`1/(k+rank)`, k=60)만 더한다 -
"다른 arm 하나가 더 찾아준 것"과 같은 크기라, 관련도가 낮은 이웃이 정답을 밀어내지 못한다.
씨앗은 병합 랭킹 상위 `_LINK_SEED_TOP_N`(=10, 기본 `limit`과 같은 값). 링크 응집도는
**필터/anchor 앞**에서 돈다 - 후보 집합을 바꾸지 않고 순서만 바꾸므로 교집합의 결과 집합은
그대로다.

### 실측 (v0, 76질의, `--no-llm`, 358문서)

baseline `ff9c639310e446a9ad62c70d1de32a01` vs treatment `cab059bec01448f9b6e8d2b6265e4183`
(연속 실행 페어): ndcg@10 0.8490 → 0.8514, delta **+0.0024**, 95% CI [-0.0004, +0.0076]
**유의하지 않음**. win/loss/tie = **1/1/68**.
- `v0-sequence-001`("7월 29일 risk-agent 아웃테이지 감지부터 복구 확인까지의 알림 순서")
  0.000 → **0.177**: 같은 채널 `#C0BGQEQG1L6`의 정답 에피소드가 후보 하위에서 상위 10 안으로.
- `v0-sequence-003` 1.000 → 0.990: 같은 채널의 오답 1건이 4~5위로 끼어든 만큼.

즉 **2건을 결정론적으로 바꾸고 68건은 그대로다.** 평균 이득은 ±0.056 노이즈 바닥 안이라
"유의하다"고 말할 수 없다 - 값어치는 평균이 아니라 "손실이 1건, 그 손실이 -0.010"이라는 데 있다.

## metadata_filter를 언제 믿는가 (`merge()` 분기 판단 기준)

`_is_source_hint_only_filter`가 세운 "술어의 종류로 가른다"는 기준을 교집합 여부에도 적용한
결과, 오늘의 판단 기준은 이렇다.

| 필터 WHERE의 내용 | 관련도 랭킹이 있을 때 | 교집합이 0일 때 |
|---|---|---|
| 구조적 술어(작성자/참여자/시간범위/제목종류)가 하나라도 있음 | **교집합** (`intersected`) | **필터 집합을 그대로** (`no_overlap_fallback_to_filter`) |
| `source_type` 힌트뿐 | **필터를 아예 안 씀** (`source_hint_not_applied`) | 〃 |
| WHERE가 아무것도 안 걸림 | 무시 (`filter_empty_ignored`) | 〃 |

근거는 "그 술어가 틀릴 수 있는가"다. "nonghyup.com 도메인 발신자"나 "2026년 7월"은 질의가
명시한 사실이라 틀릴 수 없고, 그런 질의는 조건을 걷어내면 semantic_query가 "보낸" 한 단어로
쪼그라들어 관련도 arm 쪽이 오히려 쓰레기다. 반대로 `source_type`은 질의 표면 어휘로 찍은
추측이고 매핑 방향이 자주 반대다(코퍼스 실측: "드라이브" 언급은 gmail 11 / slack 4 /
google_drive 3).

### `filter_only`는 도달 불가능해졌다 - 그러나 고칠 이유가 없다

`filter_only`의 조건은 `not candidate_sources`, 즉 "관련도 arm이 하나도 못 찾았다"이다.
`keyword_arm`이 상시화된 뒤로 semantic_query가 있는 질의는 제목/본문 조회가 최소 한 건은
물어오므로 이 조건이 성립하지 않는다 - v0에서 `metadata_filter`가 돈 **30건 중 `filter_only`는
0건**이다(예전 baseline 5건).

그런데 **v0에서 그 때문에 손해 보는 질의는 없다.** 실측(run `ff9c6393`): 구조적 술어가 있는
8건(temporal 3, relative_temporal 3, `metadata_filter-001`, `aggregation-003`)은 전부
`intersected`(6건) 또는 `no_overlap_fallback_to_filter`(2건)로 가서 **ndcg@10 1.000**이다.
낮은 점수를 받은 필터 질의(`set_query-001` 0.494, `latest-002` 0.500 등)는 전부
`source_hint_not_applied` 쪽이고, 원인은 교집합이 아니라 그 질의의 관련도 랭킹 자체다.

`count` 쪽도 마찬가지다. SQL COUNT는 `filter_only`/`no_overlap_fallback_to_filter`에서만
쓰는데, v0의 count 질의 3건 중 `aggregation-003`은 `document_ids`로 이미 정답(11)이고,
`aggregation-002`("Google Drive 공유 요청 메일은 총 몇 통인가", 정답 23)는 필터가
`source_type IN ('gmail','google_drive')`라 SQL COUNT를 쓰면 **59**가 나와 더 나빠진다.

결론: 조건을 "관련도 arm에 독립 신호가 없다" 같은 것으로 되살리는 변경은 **이득 0, 위험 > 0**이다
(어휘 arm이 정답을 제대로 찾은 질의에서까지 랭킹을 버리고 최신순 필터 집합을 답으로 낼 수 있다).
분기는 안전망으로 남긴다 - 임베딩이 없거나 `strategies_override`로 arm을 끈 실행에서는 여전히
도달하고, 그때 답이 통째로 비는 것을 막는다. **조건이 낡은 대리 지표라는 사실 자체는 코드
주석에 박아 두었다** - 나중에 arm 라우팅을 건드리는 사람이 이 분기가 조용히 되살아나는 것을
알아채야 하기 때문이다.
## 제목은 문서 종류 라벨이 아니다 — `title_types` 술어 제거 (2026-09-05)

`_metadata_filter_where`는 `document_types` 중 소스로 매핑되지 않는 라벨("회의록"/"매뉴얼"/
"보고서"/"가이드"…)을 `d.title ILIKE '%매뉴얼%'` 술어로 걸었다. **이 술어는 제목을 타입
라벨로 쓴다는 잘못된 전제 위에 있었다.** 제목은 사람이 읽는 문자열이라, 거기에 본문이 조금이라도
섞이는 순간 오탐이 된다. 게다가 그 필터는 `_is_source_hint_only_filter`가 "믿을 만한 구조적
술어"로 분류하는 쪽이라 **교집합**을 잡는다 — 오탐 1건이 관련도 랭킹 전체를 대체한다.

실측 붕괴(`fix/slack-channel-names`가 슬랙 제목에 첫 메시지 발췌 120자를 붙였을 때):

| 질의 | 지표 | 발췌 전 | 발췌 후 | 원인 |
|---|---|---|---|---|
| `v0-whole_document_summary-003` | ndcg@10 | 0.631 | 0.000 | `%매뉴얼%`에 "[done] 운영 매뉴얼 초안" 스레드가 걸림 |
| `v0-negative-001` | none_correct | 1.000 | 0.000 | `%가이드%`("Kubernetes 클러스터 구성 가이드") |

**negative 질의까지 깨진다**는 것이 이 결함의 성격을 가장 잘 보여준다: 정답이 없어야 하는
질의에 오탐 하나가 교집합을 잡아 답으로 나갔다.

### "신뢰할 수 있는 출처로 옮긴다"를 먼저 시도했고 기각한 근거

개발 DB(358 docs) 실측:

- **`documents.metadata`에 `document_type` 키가 없다.** 358행 전부. 있는 키는
  `created_at`/`page_count`/`author`/`language`/`title`/`version_*`뿐이다.
- **`knowledge_objects.document_type`은 있지만 하드 필터의 입력으로 못 쓴다.** LLM이 매번
  자유 문자열로 채운 값이라 5107행에 서로 다른 값이 **571가지**다("Slack 메시지" 33 /
  "Slack 대화" 23 / "Slack 메시지 스레드" 17 / "슬랙 메시지" 3 / "Slack message" 7 /
  "guide" 4 / "guide or manual" 2 …). `DOCUMENT_TYPE_MARKERS`의 8개 라벨로 정규화할 방법이
  없고, 정규화한들 KO가 붙은 문서는 **263/358**뿐이라 나머지 95건이 "타입 불명"이 아니라
  "타입 불일치"로 하드 필터에서 **통째로 사라진다.**

그래서 술어 자체를 없애고, `_is_source_hint_only_filter`의 "구조적 술어" 목록에서도 뺐다
(둘은 1:1 대응이라 같이 고쳐야 한다). 함께 `_has_metadata_predicate` 가드를 넣었다 —
`suggest_strategies`는 `document_types`만 있어도 metadata_filter를 고르는데, 이제 그 라벨은
술어를 만들지 않으므로 WHERE가 `project_id` 하나뿐인 질의가 코퍼스 전체를 긁어와 아무것도
안 거르는 교집합을 잡는 일이 생긴다. 그런 실행은 아예 건너뛰고 `skipped="no_predicate"`를
step에 남긴다.

**잃은 것은 하드 필터로서의 타입 조건뿐이다.** 타입 라벨은 그동안에도 `semantic_query`를 통해
제목/본문/벡터 arm에 들어가고 있었다("회의록 요약해줘"의 "회의록"은 `document_search`가 본다).
남은 과제: 타입 라벨을 **관련도 신호**로 쓰는 길(예: KO `document_type`을 임베딩 텍스트에
섞기)은 열려 있다 — 하드 필터가 아니므로 커버리지 구멍이 치명적이지 않다.

## term fallback 게이트: "히트가 있나"가 아니라 "히트가 강한가" (2026-09-05)

`keyword_arm`은 semantic_query 통째로 건 제목/본문 조회가 **빈손일 때만** term 단위
fallback으로 내려갔다(`if not title_hits and not body_hits`). 그래서 **약한 1히트**가 게이트를
닫아 그 1건이 결과 전부가 됐다 — `v0-multihop-002` 1.000 → 0.613(기준선에서는 0히트라
fallback이 돌아 gmail 정답까지 데려왔는데, 슬랙 제목에 발췌가 붙어 1건이 잡히자 게이트가 닫혔다).

게이트를 **없애면 안 된다**: 항상 fallback을 돌리면 제목이 정확히 맞은 질의를 term 히트가
밀어낸다(`v0-navigational-002` "codex README" 1.000 → 0.356). 그래서 판정 기준만 바꿨다.

```python
arm_documents = len({hit["document_id"] for hit in [*title_hits, *body_hits] if hit.get("document_id")})
if self.anchor is None and arm_documents < _ARM_STRONG_DOCUMENTS:   # = 2
    await self._term_fallback()
```

- **히트 수가 아니라 문서 수**를 센다. 한 문서의 청크 여러 개는 "여러 후보"가 아니다.
- **임계는 일부러 최소값(2)**이다. 서로 다른 문서 1건은 애초에 랭킹이 아니다(비교 대상이
  없어 RRF가 할 일이 없고, `lexical_search`의 3단 매칭에서 그 1건은 대개 마지막
  `word_similarity` 티어로 겨우 걸린 것이다). 2건 이상이면 이미 순서가 있는 후보 집합이라,
  거기에 term별 소스 열 개를 얹으면 **약한 히트를 살리려다 강한 히트를 밀어낸다.**
  이 프로젝트가 같은 실패 양식을 세 번 밟았다: `latest` 후처리(후보가 늘수록 나빠짐),
  게이트 없는 링크 확장(`feature/links-ranking`: `whole_document_summary-003` 0.631→0.000,
  `factoid-003` 1.000→0.289), 그리고 무조건 term fallback. **RRF 표를 문서 자신의 관련도
  순위와 무관한 크기로 주면 랭킹이 붕괴한다** — 임계를 올리자는 제안은 이 실측을 먼저 봐라.
- **anchor를 먼저 본다.** 제목 substring 완전 일치는 가장 강한 신호인데, `_narrow_to_anchor`가
  히트를 그 문서 하나로 좁히므로 문서 수는 1이 된다. anchor 가드가 없으면
  `v0-navigational-002`가 그대로 회귀한다.

## `participant_names`: 슬랙 참여자 이름 (2026-09-05)

`_metadata_filter_where._person()`은 `sources.metadata->>'participants'`와
`documents.metadata->>'author'`만 봤다. `participants`의 모양은 소스마다 다르다 — gmail은
`"이름 <주소>"` 문자열이라 이름이 그대로 들어 있지만, **slack은 유저 ID 배열(`["U0…"]`)이라
사람 이름이 절대 닿지 않는다.** `SlackDirectory`가 그 ID를 풀어 넣은
`sources.metadata.participant_names`에 OR를 하나 얹었다(원시 `participants`는 스레드
그룹핑·링크 백필의 조인 키라 그대로 남는다).

개발 DB 확인: `sources.metadata.participant_names`는 slack 소스 **197/197행 전부** 채워져
있다(`["intel", "백상이(패트릭)", "안민균(베르)"]` 같은 모양). 즉 데이터는 이미 있고 코드만
안 보고 있었다.

### 랭킹 구조 수정 A/B와 채택 결과 (2026-09-14)

`feature/search-ranking`의 7개 커밋을 골든셋 v1 두 벌(chunk / knowledge_object)의 dev split으로 **커밋마다 바로 앞 커밋과** 짝지어 쟀다. 지표는 항목별 대표 점수(lookup ndcg@10 / exhaustive recall / none_correct)의 평균이다.

| 커밋 | chunk | KO | 결정 |
|---|---|---|---|
| grain 명시 실행에서 문서 단위 히트를 grain 단위로 펼치기 | +0.099 [+0.052, +0.153] | +0.051 [+0.006, +0.100] | 채택 |
| 시간 범위가 arm을 전부 0건으로 만들면 풀기 | −0.007 (1승 2패) | −0.007 | 퇴행 수정 후 채택 |
| 문서 단위 RRF와 질의별 소스 분리 | +0.020 (20승 14패) | −0.013 (19승 23패) | 제외 — 유의하지 않고 방향이 엇갈림 |
| wide 질의 anchor는 고정만, 좁히지 않기 | 0 | 0 | 채택 — 골든셋에 해당 질의가 없어 프로브("한화손해보험 관련 문서 전부" 2→7문서)로 검증 |
| term fallback coverage를 문서별 SQL로 | **−0.018 [−0.039, −0.002]** | −0.004 (0승 3패) | 제외 — 유의한 손실 |
| 시간 범위만으로 출처 힌트를 하드 필터로 올리지 않기 | −0.014 (0승 2패) | 0 | 제외 |
| semantic_query가 빈 wide 질의에 원문 keyword_arm | 0 | 0 | 제외 — 근거 없음, 정밀도 낮음 |

제외한 커밋에서 배운 것:
- **coverage를 정확히 세자 노이즈가 늘었다.** `keyword_terms`가 "어떤", "위해", "하나요", "알려주세요" 같은 질문 어미를 term으로 남기는데, 예전 top-20 절단이 우연히 이들을 걸러 주고 있었다. 정답 없는 질의가 1.0 → 0.0으로 떨어졌다. 진짜 수정은 term 추출의 불용어 처리다(미구현).
- **시간 완화의 퇴행**: "2026년 6월 메일 문서 전부"처럼 필터 집합이 곧 답인 wide 질의에서도 완화가 켜져 1.0 → 0.0. wide 질의에서 필터가 문서를 찾았으면 완화하지 않도록 고쳤다.
- **출처 힌트 강등의 퇴행**: "슬랙 채널에서 2026년 7월 문서 전부"가 필터 집합 대신 시간만 건 교집합을 내 1.0 → 0.0.

채택한 3개(+퇴행 수정)를 main 위에 다시 쌓은 최종 후보 vs main:

| split | chunk 종합 | chunk lookup ndcg@10 | KO 종합 | KO lookup ndcg@10 |
|---|---|---|---|---|
| dev (n=141) | +0.106 [+0.058, +0.160] | +0.065 [+0.020, +0.115] | +0.058 [+0.011, +0.108] | +0.011 [−0.032, +0.058] |
| test (n=140) | **+0.123 [+0.069, +0.179]** | **+0.080 [+0.034, +0.130]** | **+0.046 [+0.003, +0.094]** | −0.010 [−0.044, +0.028] |

읽는 법:
- 종합 점수의 이득은 대부분 exhaustive recall이다(test chunk 0.049 → 0.805). 필터·제목이 찾은 문서가 grain 채점 리스트에 들어가게 됐다.
- **grain 펼치기는 `retrieve(grain=...)`를 명시할 때만 켜진다** — 평가와 e2e가 그 경우다. 에이전트 `search` 도구(grain 미지정)의 동작을 바꾸는 채택분은 시간 완화와 wide anchor 둘뿐이다.
- lookup은 승보다 패가 많다(test KO 4승 13패). 문서당 3개씩 끼워 넣는 grain 단위가 다른 정답을 밀어내는 것으로 보이며, 끼워 넣는 개수·유사도 하한 조정이 후속 과제다.
