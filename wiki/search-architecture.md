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
| ① Query Understanding (intent/entity/temporal) | **부분 구현** — temporal expression parsing(`search/temporal.py`)만 있음, 아래 절 참고. intent classification/entity extraction/conversation-aware query는 여전히 LLM 판단에 위임(설계만) |
| ② Query Planning (multi-hop 분해) | **구현됨** — `search/planner.py`, 아래 절 참고 |
| Query Routing (소스 선택) | **구현됨** — `search/router.py`, 아래 절 참고 |
| ③ Hybrid Retrieval (BM25+Vector+Metadata+Graph) | **부분 구현** — keyword(`file_search`)+semantic(`vector_search`)+metadata(`project_id`/`type`/시간 범위) 병행 실행 + RRF reranking까지 있음, graph는 미구현 |
| Multi-stage Retrieval / Reranking | **부분 구현** — `search/rerank.py`의 Reciprocal Rank Fusion, 아래 절 참고. Cross-Encoder/LLM reranker는 미구현 |
| ④ Context Engineering (dedup/conflict/compression) | **부분 구현** — 토큰 예산 압축(`agent/compaction.py`), cross-source dedup(`search/rerank.py`, knowledge_object_id 기준)은 있음. conflict 탐지/source reliability/temporal consistency/evidence clustering은 없음 |
| ⑤ Map-Reduce / Hierarchical Retrieval | 설계만, 미구현 |
| ⑥ Temporal Search (자연어 시간 표현 → 범위 필터) | **부분 구현** — `search/temporal.py`, 아래 절 참고. Text-to-SQL/서로 다른 KnowledgeObject 타입 간 temporal join(예: "지난달 결정 중 미완료인 것")은 미구현 |
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
폴백(먼저 것이 0건이면 다음 것)이 아니라, 목록에 있는 소스를 **전부** 병행 실행해서
`search/rerank.py`로 병합한다(아래 ③ 참고) - 소스 선택은 라우터, 병합/재정렬은 rerank 모듈로
역할이 나뉜다.

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

## ④ Context Engineering — 부분 구현

`agent/compaction.py`가 **토큰 예산** 관점의 압축(오래된 turn을 요약으로 치환)은 이미 하고
있고, evidence 단위에서는 `search/rerank.py`의 **cross-source dedup**(같은 `knowledge_object_id`
를 가진 hit을 하나로 합침, 위 ③ 참고)까지는 구현되어 있다. 하지만 RAG 맥락에서 말하는 나머지
context engineering(conflicting information 탐지, source reliability 평가, temporal
consistency, relevance filtering, evidence clustering)은 없다. 지금 규모(`plan_search` 하위
질의당 최대 5건, 소스 최대 2개)에서는 모델 컨텍스트 윈도우에 그대로 넣어도 감당 가능하다고 보고
미룬 것 - evidence 개수/소스 수가 늘어나면 가장 먼저 필요해질 레이어.

## ⑤ Map-Reduce / Hierarchical Retrieval — 설계만, 미구현

"지난 1년간 회사 AI 전략 변화 요약" 같은, 문서 수천~수만 개를 partition→map→reduce하거나
Message→Chunk 요약→Document 요약→Project 요약처럼 계층적으로 사전 요약해 둬야 하는 질문은 지금
스코프 밖이다. `pipeline.py`가 이미 문서 단위 map-reduce 요약(`extraction/schemas.py`의
`ExtractionResult.summary`를 모아 재요약)을 고민한 흔적이 `roadmap.md`(7단계 이전 문서 요약
설계)에 있으니, 필요해지면 그 설계를 확장하는 방향이 될 것.

## ⑥ Temporal Search — 부분 구현, `search/temporal.py`

"어제", "지난주", "이번 분기", "2025년 Q3", "최근 3일" 같은 상대/분기 시간 표현을
`parse_temporal_expression(text, now=None) -> TimeRange | None`이 명시적 `[start, end)` UTC
범위로 바꾸고, `file_search`/`vector_search`가 그 범위로 결과를 필터링한다.

- **LLM 호출이 아니라 정규식 + 달력 산술**이다 - `search/router.py`와 같은 이유: "어제"를 explicit
  range로 바꾸는 건 `now`가 고정되면 완전히 기계적인 계산이라, 여기에 LLM 라운드트립을 쓰면
  정확도 개선 없이 지연/비용만 늘어난다.
- **필터 적용 지점**: `file_search`는 frontmatter의 `generated.at`(리소스는 `Source.collected_at`,
  오브젝트는 `KnowledgeObject.extracted_at`이 그 값)을, `vector_search`는
  `knowledge_objects.extracted_at`을 기준으로 `[since, until)` 필터를 건다 - 방문/파이프라인
  처리 시각이지 "그 일이 실제로 일어난 시각"(예: Slack 메시지의 실제 작성 시각)은 아니다.
  이 저장소에서 KnowledgeObject 레벨에 저장된 유일한 타임스탬프가 이것이라 어쩔 수 없이 쓰는
  근사치 - Slack collector 등이 실시간에 가깝게 수집한다면 오차가 작겠지만, 배치로 오래된
  데이터를 한꺼번에 넣으면 "지난주"가 실제 사건 시점과 어긋날 수 있다.
- **어디서 자동으로 쓰이는가**: (1) `plan_search`(`search/planner.py::_execute_subquery`)가 각
  하위 질의 문장 자체를 스캔해 시간 표현을 찾으면 자동으로 그 하위 질의의 검색에 적용한다 -
  모델이 별도 인자를 신경 쓸 필요가 없다. (2) `file_search`/`vector_search` 도구 자체도
  선택적 `time_range`(자연어 문자열) 인자를 받는다 - 모델이 직접 이 두 도구를 호출할 때도
  "어제" 같은 표현을 그대로 넘기면 되고, 날짜 계산은 여기(`search/temporal.py`)가 대신한다.
  인식하지 못하는 표현은 에러 없이 그냥 무시된다(필터 없이 검색).
- **타임존**: 항상 UTC 기준(`KnowledgeObject.extracted_at`이 항상 UTC로 저장되므로 데이터와
  일관성을 맞춘 것) - 호출자가 UTC와 먼 타임존에 있으면 "오늘"/"어제"의 자정 경계가 그 호출자의
  체감과 최대 하루 정도 어긋날 수 있다. 월요일을 주의 시작으로 본다(ISO, 한국 관례와 일치).
- **아직 안 되는 것 (Temporal Join)**: "지난달 결정된 사항 중 아직 완료되지 않은 것"처럼 서로
  다른 KnowledgeObject 타입(Decision과 ActionItem 등)에 각각 다른 조건(시간 범위 + 상태)을
  걸어 join하는 건 여전히 미구현이다 - `content: dict`가 타입별 자유 스키마라 "완료 여부"
  같은 상태 필드가 구조화된 필터로 걸 수 있게 정규화되어 있지 않은 게 근본 원인(Text-to-SQL을
  만들어도 걸 조건 자체가 아직 없다). 시간 범위 하나로 한 소스를 필터링하는 것까지가 이번
  구현의 범위.

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

바로 이어서, 그 순서의 첫 항목인 **③ Multi-stage Retrieval/Reranking**(RRF, `search/rerank.py`)
과 거기서 자연스럽게 같이 풀리는 **④ cross-source dedup**을 구현했다 - `plan_search`가 하위
질의당 두 소스를 병행 실행하기 시작한 순간부터 "결과를 그냥 이어붙이기만 한다"는 게 가장 먼저
체감되는 품질 문제였기 때문(같은 KnowledgeObject가 두 번 나오거나, 두 소스 다 낮게 평가한
결과와 둘 다 동의한 결과가 구분 없이 뒤섞임).

이어서 **⑥ Temporal Search**(`search/temporal.py`)를 구현했다 - "어제 뭐 있었어" 같은 질문이
지금까지 전혀 필터링되지 않고 그냥 키워드/의미 검색으로만 처리되는 게 가장 체감 격차가 큰
남은 gap이었다. Text-to-SQL이나 서로 다른 KnowledgeObject 타입 간 temporal join까지는 손대지
않았다 - 걸 조건(예: "완료 여부") 자체가 구조화되어 있지 않아 그 확장은 스키마 변경 없이는
의미가 없다고 판단했다(위 절의 "아직 안 되는 것" 참고). 남은 순서(⑤ map-reduce → citation →
feedback loop, 그리고 ④의 나머지인 conflict 탐지/reliability/clustering)는 여전히 설계만.
