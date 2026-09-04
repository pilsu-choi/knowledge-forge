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
| ① Query Understanding (intent/entity/temporal) | 설계만, 미구현 — 아래 절 참고 |
| ② Query Planning (multi-hop 분해) | **구현됨** — `search/planner.py`, 아래 절 참고 |
| Query Routing (소스 선택) | **구현됨** — `search/router.py`, 아래 절 참고 |
| ③ Hybrid Retrieval (BM25+Vector+Metadata+Graph) | **부분 구현** — keyword(`file_search`)+semantic(`vector_search`)+metadata(`project_id`/`type`) 병행 실행까지는 있음, graph는 미구현, reranking 없음 |
| Multi-stage Retrieval / Reranking | 설계만, 미구현 |
| ④ Context Engineering (dedup/conflict/compression) | **부분 구현** — 토큰 예산 압축(`agent/compaction.py`)만 있음, evidence 단위 dedup/conflict 탐지는 없음 |
| ⑤ Map-Reduce / Hierarchical Retrieval | 설계만, 미구현 |
| ⑥ Temporal Search (Text-to-SQL/Temporal Join) | 설계만, 미구현 |
| Query Routing 확장 (SQL/Web 소스) | 설계만, 미구현 (현재 소스는 file_search/vector_search 둘뿐) |
| Evidence Grounding / Citation | **약하게 구현** — 시스템 프롬프트가 path/knowledge_object_id 인용을 지시할 뿐, claim-level 매칭/인용 파이프라인은 없음 |
| Search Evaluation / Feedback Loop | 설계만, 미구현 |
| Iterative / Agentic Retrieval | **이미 구현됨** — 별도로 만들 필요가 없었다, 아래 절 참고 |

---

## ① Query Understanding — 설계만, 미구현

Intent classification(사실 조회/비교/요약/탐색/분석), entity extraction(사람/프로젝트/문서),
query rewriting/expansion(모호한 질문 → 검색 친화적 질의, 동의어/약어 확장), conversation-aware
query("그 프로젝트는?" → 이전 대화의 entity 복원)까지 포함하는 넓은 레이어. 지금은 이 전체를
LLM(에이전트 자신)에게 맡기고 있다 — `knowledge-forge agent`는 매 턴 전체 대화 히스토리를 보고
스스로 file_search/vector_search/plan_search 중 무엇을, 어떤 질의로 호출할지 판단한다
(`agent/prompts.py`). 별도 전처리 단계로 뽑아내지 않은 이유는, 지금 규모(도구 2~3개, 명시적
멀티턴 세션)에서는 에이전트 자신의 판단이 이미 이 레이어의 상당 부분(특히 conversation-aware
query, temporal understanding)을 공짜로 해주기 때문 — 별도 파이프라인을 만들면 에이전트가 이미
하는 일을 중복으로 다시 하게 된다. Query Understanding을 명시적 레이어로 뽑아낼 필요가 생기는
시점은, (a) 에이전트 turn 없이 검색 API를 직접 호출하는 소비자(HTTP `/search/*`, MCP 서버)가
생기거나, (b) temporal join처럼 LLM 판단만으로는 부정확한 구조화된 파싱이 필요해질 때
(아래 ⑥ 참고).

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
폴백(먼저 것이 0건이면 다음 것)이 아니라, 목록에 있는 소스를 **전부** 병행 실행해서 합친다(간이
hybrid). 두 소스가 같은 KnowledgeObject를 각자 다르게 찾아도 **cross-source dedup은 하지 않는다**
- `file_search`는 OKF 파일 `path`로, `vector_search`는 `knowledge_object_id`로 결과를 식별해서
서로 다른 식별자 공간이라 안전한 매칭 키가 없다(`file_search`가 frontmatter의
`knowledge_object_id`를 응답에 아직 노출하지 않음). 두 소스가 같은 대상을 각각 찾으면 evidence
목록에 중복으로 나타날 수 있다는 뜻 - 상위 에이전트가 종합할 때 알아서 걸러내는 것에 기대고
있다. 진짜 dedup/rerank는 ③/④ 레이어(아래) 스코프.

## Iterative / Agentic Retrieval — **이미 구현됨**, 새로 만들 필요 없음

"한 번 검색하고 끝내지 않고, 근거가 부족하면 재검색"이라는 요구사항은 `agent/loop.py::run_agent`
의 도구 호출 루프 자체가 이미 제공한다 — 에이전트는 file_search/vector_search/plan_search 결과를
보고 부족하다고 판단하면 같은 턴 안에서 다른 질의로 다시 호출할 수 있고(최대
`KF_AGENT_MAX_ITERATIONS`회), `write_plan`으로 여러 단계 조사 계획을 스스로 세울 수도 있다
(`agent-runtime.md`). 별도의 "충분한가?" 판단 모듈이나 refinement 루프를 새로 만들지 않은 이유는
이게 이미 tool-calling 에이전트의 기본 동작이기 때문 - 추가로 만들면 에이전트가 이미 하는 일을
어설프게 다시 구현하는 셈이 된다.

## ③ Hybrid Retrieval — 부분 구현

- Keyword(`file_search`, substring/frontmatter) + Semantic(`vector_search`, pgvector) 병행
  실행은 Query Routing을 통해 이미 가능(위 참고).
- Metadata/entity filtering(`project_id`/`type`)도 두 도구 모두 1급 파라미터로 이미 지원한다 -
  "김철수가 참여한 Project A 관련 논의"처럼 metadata 필터가 semantic similarity보다 중요한
  경우, 에이전트가 `project_id`/`type` 인자로 좁혀 호출할 수 있다(단, "김철수가 참여한"이라는
  **entity 필터**는 KnowledgeObject 스키마에 참여자 필드가 정규화되어 있지 않아 아직
  구조화된 필터로 걸 수 없다 - `content`/`topics`의 자유 텍스트 매칭에 의존).
- Graph 축(`GET /relations/expand`, `agent-search.md`)은 여전히 미구현.
- **Reranking은 없다** - 두 소스의 결과를 점수 기준으로 통합 재정렬하는 Cross-Encoder/LLM
  reranker, Multi-stage retrieval(candidate → rerank → evidence selection)은 설계만 있고
  미착수. 지금은 각 소스가 이미 자기 기준(BM25 스타일 가중치 매칭/cosine 유사도)으로 정렬해
  반환한 것을 그대로 합쳐 넘긴다.

## ④ Context Engineering — 부분 구현

`agent/compaction.py`가 **토큰 예산** 관점의 압축(오래된 turn을 요약으로 치환)은 이미 하고
있지만, RAG 맥락에서 말하는 evidence 단위의 context engineering(중복 evidence 제거, conflicting
information 탐지, source reliability 평가, temporal consistency, relevance filtering, evidence
clustering)은 없다. 지금 규모(`plan_search` 하위 질의당 최대 5건, 소스 최대 2개)에서는 모델
컨텍스트 윈도우에 그대로 넣어도 감당 가능하다고 보고 미룬 것 - evidence 개수/소스 수가 늘어나면
가장 먼저 필요해질 레이어.

## ⑤ Map-Reduce / Hierarchical Retrieval — 설계만, 미구현

"지난 1년간 회사 AI 전략 변화 요약" 같은, 문서 수천~수만 개를 partition→map→reduce하거나
Message→Chunk 요약→Document 요약→Project 요약처럼 계층적으로 사전 요약해 둬야 하는 질문은 지금
스코프 밖이다. `pipeline.py`가 이미 문서 단위 map-reduce 요약(`extraction/schemas.py`의
`ExtractionResult.summary`를 모아 재요약)을 고민한 흔적이 `roadmap.md`(7단계 이전 문서 요약
설계)에 있으니, 필요해지면 그 설계를 확장하는 방향이 될 것.

## ⑥ Temporal Search — 설계만, 미구현

"어제", "지난주", "Q2" 같은 상대 시간 표현을 명시적 시간 범위로 바꾸고, `Decision.created_at ∈
last_month AND Task.status != completed`류의 temporal join까지 하려면 Text-to-SQL 또는 구조화된
temporal query parser가 필요하다. 지금은 `knowledge_objects.extracted_at`이 있으니 데이터는
있지만, 이를 질의로 바꾸는 계층이 없다 - 에이전트가 자연어로 "어제"를 이해하고 있다 해도, 그걸
정확한 `WHERE extracted_at BETWEEN ...` 필터로 실행할 방법이 `file_search`/`vector_search`
어느 쪽에도 없다(`file_search`/`vector_search` 모두 시간 필터 파라미터가 없다). 우선순위가
낮은 이유: 지금 KnowledgeObject 볼륨에서는 "최근 N개"보다 정확도가 시급한 문제가 아니었다.

## Evidence Grounding / Citation — 약하게 구현

`agent/prompts.py`가 "근거로 삼은 검색 결과의 path/project_id(또는 knowledge_object_id)를
인용할 것"을 지시하지만, 이건 프롬프트 수준의 요청이지 강제되는 파이프라인이 아니다 - 답변에서
claim을 추출해 evidence와 자동으로 매칭/인용하는 별도 단계(Claim extraction → Evidence matching
→ Citation)는 없다. 모델이 지시를 무시하면 인용 없는 답이 나올 수 있다.

## Search Evaluation / Feedback Loop — 설계만, 미구현

Recall/Precision/MRR/NDCG, answer groundedness, citation accuracy 같은 지표도, 쿼리별 실패
원인 추적도 없다 - 이 시스템에는 아직 "검색이 잘 됐는지" 평가할 방법이 코드로 존재하지 않는다.
가장 먼저 필요해지는 시점은 실사용 트래픽이 쌓여 어떤 질의 유형이 자주 실패하는지 알아야 할 때.

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
