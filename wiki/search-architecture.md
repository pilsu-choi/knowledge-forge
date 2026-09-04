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
| ① Query Understanding (intent/entity/temporal) | **부분 구현** — `search/constraints.py`가 질의를 구조화된 제약(`QueryConstraints`: 시간 범위/작성자/식별자/문서 참조/소스·문서 타입)과 의도 플래그(latest/exhaustive/count/summary/whole_document/negative/comparison/multihop)로 분해하고 `suggest_strategies()`로 검색 전략 이름을 내놓는다(규칙 1단계 + 선택적 LLM 보강 2단계). temporal parsing(`search/temporal.py`)을 재사용. 그 전략 목록을 실제로 실행해 하나의 랭킹으로 합치는 실행기가 `search/retrieve.py`(아래 "Retrieval Plan 실행기" 절, 에이전트 `search` 도구와 `eval-ir`가 공유). 아직 없는 것: conversation-aware 질의 복원, 동의어/약어 확장 |
| ② Query Planning (multi-hop 분해) | **구현됨** — `search/planner.py`, 아래 절 참고 |
| Query Routing (소스 선택) | **구현됨** — `search/router.py`, 아래 절 참고 |
| ③ Hybrid Retrieval (BM25+Vector+Metadata+Graph) | **부분 구현** — keyword(`file_search`)+semantic(`vector_search`)+metadata(`project_id`/`type`/시간 범위) 병행 실행 + RRF reranking까지 있음. 문서 제목/원문 청크의 pg_trgm 렉시컬 검색(`document_search`/`lexical_search`)도 있음. Graph 전용 API(`graph_search`)는 미구현이지만 `sql_search`로 `relations` 테이블을 직접 JOIN하는 것으로 상당 부분 대체 가능 |
| Multi-stage Retrieval / Reranking | **부분 구현** — `search/rerank.py`의 Reciprocal Rank Fusion, 아래 절 참고. Cross-Encoder/LLM reranker는 미구현 |
| ④ Context Engineering (dedup/conflict/compression) | **부분 구현** — 토큰 예산 압축(`agent/compaction.py`), cross-source dedup(`search/rerank.py`, knowledge_object_id 기준)은 있음. conflict 탐지/source reliability/temporal consistency/evidence clustering은 없음 |
| ⑤ Map-Reduce / Hierarchical Retrieval | 설계만, 미구현 |
| ⑥ Temporal Search (자연어 시간 표현 → 범위 필터 / Text-to-SQL) | **구현됨** — `search/temporal.py`(자연어→범위 필터)와 `search/sql.py`(Text-to-SQL, `sql_search` 도구)로 시간 범위와 속성값 조건을 SQL WHERE/JOIN 하나로 결합하는 temporal join까지 가능해졌다. 아래 절 참고 |
| Query Routing 확장 (SQL/Web 소스) | **부분 구현** — SQL 소스는 `sql_search`로 추가됐다(단, `search/router.py::route_query`의 자동 라우팅 대상에는 아직 없음 - 모델이 직접 판단해서 호출). Web 소스는 여전히 미구현 |
| Evidence Grounding / Citation | **약하게 구현** — 시스템 프롬프트가 path/knowledge_object_id 인용을 지시할 뿐, claim-level 매칭/인용 파이프라인은 없음 |
| Search Evaluation / Feedback Loop | **부분 구현** — 로깅(`search_log`/`search_feedback`)과 LLM-judge 기반 groundedness/citation 자동 평가는 구현됨, 골드셋 필요한 Recall/Precision/MRR/NDCG와 온라인 groundedness gate는 설계만 — [`search-evaluation.md`](search-evaluation.md) 참고 |
| Iterative / Agentic Retrieval | **이미 구현됨**(암묵적 판단) — 아래 절 참고. 명시적 groundedness gate로 강화하는 안은 [`search-evaluation.md`](search-evaluation.md) (D) |

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

- (a) **평가 하네스 = 에이전트 없는 소비자.** `search-evaluation.md`의 골든셋 기반 IR 지표
  (Recall/MRR/NDCG)는 "질의 → 검색 결과"를 LLM 판단 없이 같은 입력에 같은 출력으로 재현해야
  지표가 의미를 가진다. 에이전트가 매 턴 암묵적으로 하던 "이건 특정 문서 요약이다 / 작성자
  필터다 / 개수 질문이다" 판단을 결정론적 함수로 꺼내지 않으면 평가할 대상 자체가 없다.
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
(1) 골든셋 IR 평가 하네스(`eval-ir`, [`search-evaluation.md`](search-evaluation.md) (C))는 에이전트
턴 없이 질의 → `document_ids` 랭킹을 얻어야 하고, 같은 입력에 같은 출력이어야 지표 비교가
성립한다. (2) 에이전트에게 도구가 7개(document/lexical/vector/sql/read_document/plan/file)로
늘면서 "이 질의엔 뭘 먼저?"를 매 턴 다시 추론하는 비용과 편차가 커졌다 - `search` 도구
(`agent/tools.py::_make_search_tool`)는 이 실행기를 그대로 감싼 것이라, 실행기가 좋아지면
평가 지표와 에이전트 경험이 같이 좋아진다(분리돼 있으면 둘이 따로 논다). LLM은 (a) 제약 보강
(`extract_constraints`), (b) 멀티홉 분해(`plan_and_search`)에만 쓰이고 `provider=None`이면 둘 다
건너뛴다 - 결정론적 기준선.

**반환 계약** (`eval-ir`와 공유, 변경하려면 양쪽 동시에): `RetrievalResult{query, constraints
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
| `vector` | `semantic_query`로 `vector_search`(since/until 전달) → **항상** 어휘 arm(`_keyword_arm`)을 이어서 실행 | 임베딩 0행/임베더 미구성이면 빈 결과 + `error` 기록(예외 아님). 유사도가 `KF_VECTOR_MIN_SCORE`(기본 0.45) 미만인 히트는 버리고 `dropped_below_min_score`에 기록 - ANN에는 관련도 하한이 없어 "관련 문서 없음"이 정답인 질의도 후보가 찬다. 어휘 arm은 `document_search#semantic` + `lexical_search#semantic` 소스로 RRF에 합류 |
| `comparison` | `comparison_targets` 각각 `document_search`(대상 안의 시간 표현은 그 대상에만 적용), 없으면 `lexical_search` | 대상별 최상위 문서를 anchor 다음으로 최상위 고정 |
| `multihop` | provider 있으면 `plan_and_search` 근거를 한 소스로 RRF 합류 | 없으면 `skipped="no_provider"`. document_id 없는 히트(OKF file_search)는 채점 단위가 없어 버림 |
| `latest` | **관련도 상위 `KF_LATEST_RELEVANCE_POOL`(기본 5) 문서 안에서만** `content_at` 내림차순, 같은 `version_family`는 `version_sort_key`로 최신 하나만 남기고 나머지는 뒤로 | family 안에서는 버전 표기(최종 > vN > 무표기 > 초안)가 시각보다 우선 - 구버전을 나중에 다시 올린 경우 때문. pool 밖 후보는 관련도 순 그대로 뒤에 붙는다 - 후보 전체를 시각 정렬하면 "질의와 상관없이 가장 최근 문서"가 1위가 된다(아래) |
| `exhaustive`/`negative` | 소스별 limit을 200으로, 최종 컷오프 없음 | 부재 판정은 LLM 몫 - 실행기는 후보만 |
| `count` | `predicted_count` = 최종 문서 수. 필터 단독 결과면 같은 WHERE로 `SELECT COUNT(*)` | count도 exhaustive 수집을 함의 |
| `whole_document` | anchor(없으면 상위 2개 문서)를 `read_document`로 읽어 `documents_read`에, 청크를 문서 순서대로 히트 맨 앞에 | 같은 청크를 가리키던 검색 히트는 중복이라 제거 |

**키워드 fallback (vector가 빈손일 때)**:

임베딩이 없는 배포/개발 DB에서 실행기가 "아무것도 못 찾음"이 되지 않게 하는 degrade 경로다.
두 단계로 돈다.

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

두 개의 안전장치가 붙어 있다. 둘 다 실측으로 필요성이 확인된 것이다(수치는
`search-evaluation.md`의 스윕 표):

- **coverage 임계값** (`KF_TERM_COVERAGE_RATIO` 기본 2/3, `KF_TERM_COVERAGE_MIN` 기본 2):
  질의의 term을 `ceil(term수 × ratio)`개 이상 담은 문서만 후보로 남긴다. 없으면 term 하나만 스친
  문서까지 전부 후보가 되어, "관련 문서 없음"이 정답인 질의(null/negative)가 전부 무언가를
  돌려주고(`none_correct` 1.000 → 0.000) 개수 질의가 부풀었다(`count_match` 0.500 → 0.000).
- **1단계가 빈손일 때만 실행**: 질의 문자열이 통째로 걸렸으면 그게 term 하나하나보다 강한 신호다.
  항상 돌리면 제목이 정확히 맞은 질의를 term 히트가 밀어낸다(`v0-navigational-002` "codex README":
  ndcg@10 1.000 → 0.356).

term당 조회는 `_TERM_FETCH_LIMIT`(20)으로 따로 막는다 — exhaustive/negative/count 질의는
`fetch_limit`이 200이고 최종 컷오프도 없어서, term 열 개 × 200이 그대로 답에 실리면 재현율보다
정밀도를 더 깎는다.


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
  | 필터가 **어휘 힌트뿐**(`source_type IN (...)` 하나) | `source_hint_not_applied` | 필터 무시, 관련도 랭킹 그대로 |
  | 그 밖에 교집합이 있음 | `intersected:N->M` | 교집합 |
  | 그 밖에 교집합이 0 | `no_overlap_fallback_to_filter` | 필터 집합을 최신순으로 |

  **어휘 힌트뿐인 필터를 하드 필터에서 뺀 이유**(`_is_source_hint_only_filter`): `source_types`는
  `SOURCE_TYPE_MARKERS` 정규식이 질의 표면 어휘만 보고 찍은 추측이고 - LLM 보강 대상 필드도 아니다
  (`LLM_ENRICHABLE_FIELDS`) - 그 어휘 → source_type 매핑은 방향이 자주 반대다. 개발 코퍼스 실측으로
  본문에 "Google Drive"/"드라이브"를 언급하는 문서의 source_type은 **gmail 11건 / slack 4건 /
  google_drive 3건**이었다. Drive에 저장된 파일은 본문에 "Google Drive"라고 쓸 이유가 없고, Drive
  공유요청 알림 메일은 항상 쓴다. 반대로 작성자/참여자/시간/제목 종류는 질의가 명시한 구조적
  술어라 교집합이 0이어도 필터 쪽을 믿는다(자세한 근거는 wiki/search-evaluation.md의 회귀 기록).
- `limit`은 최종 `document_ids` 길이다. 소스별 조회는 3배 over-fetch해서 필터/anchor 좁히기 뒤에도
  limit개가 남게 한다(`strategies_override=["vector"]`, limit=10이면 vector 20건 → 상위 10문서 -
  순수 벡터 baseline).

**한계**:

- 임베딩이 없으면(개발 DB 기준 0행) `vector`는 항상 비고 키워드 fallback으로 돈다 - 패러프레이즈/
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
gate를 얹는 안은 [`search-evaluation.md`](search-evaluation.md) (D)에 설계해뒀다 - 아래
Search Evaluation / Feedback Loop 절과 판정기를 공유하는 옵션 확장이라 그쪽에서 같이 다룬다.

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
    항상 싣는다 - [`search-evaluation.md`](search-evaluation.md) (C) 골든셋의 채점 단위.
  - `search/router.py`: `_ID_LIKE_RE`가 못 잡던 숫자 시작/한글 혼합 식별자(`2025_SH_업무_보고서`
    - `_`/`-`로 이어진 3토큰 이상)와 확장자(.pdf/.xlsx/.docx/.pptx/.hwp…) 포함 토큰을
    `has_exact_match_signal()`의 정확 일치 신호로 추가했다. `plan_search`의 하위 질의 라우팅은
    여전히 `file_search`/`vector_search` 두 소스만 고른다 - `document_search`를 planner에 엮는
    건 골든셋으로 효과를 잰 뒤에.
  - **아직 없는 것**: `document_search` 후보를 `vector_search`/`lexical_search`의 `document_id`
    필터로 바로 잇는 파라미터(지금은 모델이 후보를 고른 뒤 `sql_search`나 본문 검색을 이어 부른다),
    Slack 채널 ID → 채널 이름 해석(제목이 `#C0BGQEQG1L6 2026-08-11` 형태로 남는다 - 컬렉터가
    `channel_name`을 메타데이터에 넣으면 파생 로직은 이미 그걸 우선 쓴다).

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
대신 `eval-ir`로 재고 나서 결정할 문제라 지금은 동작만 테스트로 고정해 두었다:

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
올바른 수정은 `_FILLER_RE`에 잔여 기능어를 더하는 쪽이고, 그건 `eval-ir` 회귀로 재고 나서 할 일.

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

## Search Evaluation / Feedback Loop — 부분 구현

`storage/eval_store.py::EvalStore`가 모든 에이전트 턴을 `search_log`에 기록하고(질의/답변/
근거/도구 호출 시퀀스/지연시간), `--interactive` REPL의 `/feedback`과 `knowledge-forge
feedback` 커맨드로 사용자 평가(👍/👎 + 코멘트)를 붙일 수 있다. `evaluation/judge.py`가
LLM-as-judge로 답변의 각 claim을 근거와 대조해 groundedness/citation 점수를 매기고
(`knowledge-forge eval` 배치, 골드셋 불필요), `eval_score`에 저장한다 - 쿼리별 실패 원인
추적의 최소 형태(사람이 `search_log`/`eval_score`를 직접 읽는 것)는 이제 가능하다.

아직 없는 것: Recall/Precision/MRR/NDCG(relevance judgment 골드셋이 있어야 정의되는 지표라
별도 사람 큐레이션이 선행돼야 함)와, 위 Iterative Retrieval을 강화하는 온라인 groundedness
gate(judge를 답변 확정 전 실시간으로 돌려 재검색을 강제하는 것 - 지금은 오프라인 배치로만
돈다). 둘 다 [`search-evaluation.md`](search-evaluation.md)의 (C)/(D)에 설계만 남아있다.

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
