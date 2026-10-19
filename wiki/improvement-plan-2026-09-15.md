# 개선 필요 사항 (2026-09-15)

main `cef6edc` 기준 진단이다. 코드는 고치지 않았다.

## 전제

- **지향점**: 팔란티어 Foundry형 구조. 사내 지식을 모아 온톨로지로 만들고, 여러 형태의 Agent가 소비한다. 검색 Agent는 첫 소비자다.
- **대상 LLM**: Qwen3.8-27B 같은 오픈소스 모델을 OpenAI 호환 엔드포인트(vLLM 등)로 붙인다. 2026-10-19에 테스트 모델을 gpt-oss-120b에서 Qwen3.8-27B로 바꿨다.
- **요구 조건**: 품질을 유지하면서 지연시간도 낮아야 한다.
- 이 전제는 `PRD.md`와 [`roadmap.md`](roadmap.md)에 없다. PRD는 Graph DB·Ontology·Agent를 MVP 제외 범위로 두었다. 그래서 아래 5장 온톨로지 항목은 결함 수정이 아니라 새 범위다.

✔ 표시는 이번에 코드로 직접 확인한 항목이다. 나머지는 병렬 리뷰가 파일 위치를 인용해 보고한 것이며 따로 재검증하지 않았다.

## 결론

- **Qwen3.8-27B를 붙이면 돌아가지만, 잘 동작한다는 근거는 없다.** 현재 main에서 오픈소스 모델로 잰 e2e 결과가 한 건도 없다.
- **에이전트 품질의 상한은 검색 파이프라인 품질이다.** 프롬프트가 search를 질문 원문으로 먼저 부르게 하고, 실측에서 150회 중 136회가 search 1회로 끝났다([`search-code-review-2026-09-14.md`](search-code-review-2026-09-14.md)).
- **지연시간은 DB가 아니라 LLM 호출 횟수와 프리필 크기가 정한다.** DB 조회는 이미 p50 141ms(임베딩 고정)까지 줄였다. 남은 비용은 질문당 LLM 호출 3~4회다.
- **품질을 올릴 싼 부품이 빠져 있다.** 리랭커·BM25·온프렘 임베딩은 LLM 호출 하나보다 싸다.
- **온톨로지는 Foundry 기준으로 아직 없다.** 엔티티 해소가 없고, 검색과 에이전트가 relations를 읽지 않는다.

## 1. 측정

| # | 문제 | 개선 |
|---|---|---|
| M1 | 현재 main에서 `eval run --e2e`가 실행된 적이 없다. 남은 e2e 수치(gpt-4o-mini 0.817, Qwen3-30B 0.266)는 삭제된 평가 코드와 `project_id` 수정 전 코드에서 나왔다 | 임베딩을 고정하고 Claude 1개 + Qwen3.8-27B 1개로 e2e를 돈다. 이것이 "검색 Agent가 잘 동작하는가"에 답하는 첫 숫자다 |
| M2 | 골든셋 v1은 gpt-4o-mini가 만들었고 사람이 검수하지 않았다(status `draft`). dev/test split도 누수가 있던 옛 방식이다 | 50~100개를 사람이 검수한 부분집합을 만들고, 새 `split_for`로 재생성한다 |
| M3 | 지연 SLO가 없다. e2e 결과에 `latency_ms`·`llm_calls` 필드는 있지만 목표값이 없다 | 종단 p50/p95와 첫 토큰까지 시간 목표를 정하고, 모든 A/B에 품질과 함께 기록한다 |
| M4 | 원격 질의 임베딩 흔들림으로 같은 코드도 562건 중 137건 결과가 달라진다. 고정·재생이 `eval run`에 연결되지 않았다 | 질의 임베딩 고정·재생을 `eval run` 옵션으로 넣는다. ±0.03 이하 차이는 고정 없이 판단하지 않는다 |
| M5 | 제약 보강 LLM 호출의 품질 기여가 측정된 적이 없다. retrieval 평가는 기본이 LLM 0회라 이 호출을 끈 채로 잰다 | 보강 켬/끔 A/B를 retrieval과 e2e 양쪽에서 잰다(3장 L2의 전제) |

## 2. 에이전트 런타임 (오픈소스 모델 대응)

런타임 구조 자체는 벤더 중립으로 잘 되어 있다. 루프 하나, 도구 오류 격리, 깨진 JSON 인자 복구, 반복 상한 도달 시 강제 답변이 있다. 부족한 것은 reasoning 모델 특유의 처리다.

| # | 문제 | 영향 | 개선 |
|---|---|---|---|
| R1 ✔ | OpenAI 호환 어댑터가 `content`와 `tool_calls`만 읽는다. `reasoning_content`/`reasoning`은 버려지고 다음 턴에 되돌려 보내지 않는다(`agent/providers/openai_compatible.py`) | reasoning 모델(Qwen thinking, gpt-oss harmony 등)의 멀티턴 도구 호출 품질 저하. 답이 reasoning 필드에만 오면 빈 문자열이 정답처럼 반환된다 | `AssistantTurn`에 reasoning 필드를 추가하고, 읽기·되돌려 보내기·빈 content일 때 대체를 넣는다 |
| R2 ✔ | `max_tokens`가 4096 고정이고 설정 키가 없다 | reasoning 토큰이 같은 예산을 먹어 답변이나 도구 호출 JSON이 잘린다. 도구 호출 중 잘리면 `finish_reason`이 무시되어 "invalid JSON arguments"로 위장된다 | `KF_AGENT_MAX_TOKENS`, temperature, seed, 요청 timeout 설정을 추가한다 |
| R3 | 도구 호출 0회로 끝난 답변을 그대로 반환한다(`agent/loop.py`) | 서빙 쪽 도구 호출 파서(vLLM `--tool-call-parser`)가 꺼져 있으면 호출이 본문 텍스트로 오고, 검색 없는 답이 아무 표시 없이 나간다 | 도구 호출 0회 답변은 한 번 재촉하거나 오류로 올린다. 본문에 `<tool_call>`·harmony 같은 마크업이나 도구 호출 JSON이 보이면 설정 오류로 알린다 |
| R4 ✔ | CLI 사전검사가 `KF_OKF_DIR`과 `OPENROUTER_API_KEY`/`OPENAI_API_KEY`를 요구한다 | 키 없는 로컬 vLLM에 붙이려면 더미 환경변수가 필요하다. OKF 경로 때문에 파일시스템 도구 4개가 항상 실린다 | base_url이 로컬이면 키 검사를 건너뛴다. `KF_OKF_DIR` 필수 조건을 없앤다. CLI docstring의 "file_search로" 문구도 현행화한다 |
| R5 | 기본 모델 문자열이 전부 Claude다(`agent_openai_compatible_model="anthropic/claude-sonnet-5"`) | `openai_compatible`로 바꾸고 모델을 빠뜨리면 조용히 Claude로 간다 | 오픈소스 배포용 설정 예시를 `.env.example`과 [`dev-guide.md`](dev-guide.md)에 둔다 |
| R6 | 400 "context length exceeded" 같은 비재시도 오류가 질의 전체를 죽인다 | 32K 로컬 서버에서 긴 검색 결과 뒤에 곧바로 실패 | 컨텍스트 초과는 압축 후 한 번 재시도한다. 압축 임계값(현재 100K 고정)을 서빙 컨텍스트 길이에 맞춘다 |
| R7 | 제약 보강·멀티홉 분해·judge의 JSON 계약이 프롬프트뿐이고, 파싱 실패는 WARNING 로그만 남기고 규칙으로 되돌아간다 | 모델을 바꿨을 때 품질 저하가 오류가 아니라 "검색이 나빠짐"으로만 보인다 | 엔드포인트가 지원하면 `response_format`을 쓰고, 되돌아간 횟수를 steps나 카운터로 드러낸다 |
| R8 | 같은 도구를 같은 인자로 반복 호출해도 막지 않는다 | 최대 8턴까지 프리필이 누적되어 p95가 폭증한다 | 동일 호출 반복 감지를 넣는다 |

## 3. 지연시간 구조

단순 질문 하나의 현재 호출 구성이다. 종단 수치는 측정된 적이 없어 비워 둔다(M1, M3).

| 단계 | 호출 | 비용 |
|---|---|---|
| 에이전트 1턴: search 호출 결정 | LLM | 프리픽스 약 6K 토큰 프리필 + reasoning |
| search 안의 제약 보강 ✔ | LLM | 검색마다 항상 1회(`retrieve(use_llm_constraints=True)`, 에이전트 도구가 provider를 항상 넘김) |
| search 안의 멀티홉 분해 | LLM | `is_multihop`일 때만. 하위 검색 추가 |
| 검색 DB + 원격 질의 임베딩 | DB/HTTP | p50 약 650ms. 임베딩 왕복이 약 300ms([`search-architecture.md`](search-architecture.md) "실행기 동시성") |
| 에이전트 2턴: 답변 | LLM | 프리픽스 6K + 검색 결과 6~7K 프리필 |

| # | 문제 | 개선 | 품질 영향 |
|---|---|---|---|
| L1 | 첫 에이전트 턴은 거의 항상 "search(질문 원문)"을 부르는 데만 쓰인다 | 하네스가 모델 호출 전에 `search(질문)`을 먼저 돌려 도구 결과로 넣는다. LLM 호출 1회가 통째로 빠진다 | 없음(이미 136/150이 그렇게 동작) |
| L2 | 제약 보강 LLM 호출이 검색마다 들어간다 | M5 결과에 따라 기본값을 정한다. 규칙만으로 확신이 서는 질의(식별자·제목 일치)는 건너뛰는 fast path를 둔다 | 측정 필요 |
| L3 ✔ | 프리픽스 약 6K 중 약 2K가 `sql_search` 설명에 박힌 한국어 SQL 스키마다(`agent/tools.py:686`의 `describe_schema()`) | 스키마를 별도 `describe_schema` 도구로 분리한다. vLLM prefix caching을 켠다 | 없음 |
| L4 | search 결과가 기본 10건 × 건당 약 650토큰이다. wide 질의는 약 20K, `read_document`는 최대 5만 자다 | 에이전트용 기본을 5~8건, 요약 300자로 줄인다. steps의 SQL 원문은 에이전트 결과에서 뺀다 | 측정 필요(e2e) |
| L5 | 스트리밍이 없다 | 최종 답변 턴을 스트리밍한다. 체감 지연은 첫 토큰까지 시간이다 | 없음 |

## 4. 검색 코어

검색 코어의 장점은 유지한다. 규칙 기반 제약 추출과 RRF가 결정론적이고 밀리초 단위다. arm 조회는 동시에 뜨고 trigram 인덱스를 탄다. steps 트레이스로 실패 원인과 비용이 보인다.

| # | 문제 | 개선 | 지연 영향 |
|---|---|---|---|
| S1 | 리랭커가 없다. RRF 뒤 정밀도를 anchor 고정·비교 대상 고정·`vector_min_score` 하한 같은 수작업 규칙이 떠받친다 | 융합 상위 약 50건에 온프렘 크로스 인코더(bge-reranker-v2-m3, Qwen3-Reranker-0.6B 등)를 붙인다. 효과가 확인되면 특수 규칙을 걷어낸다 | GPU에서 약 +100~200ms |
| S2 ✔ | BM25가 없다. 어휘 검색은 pg_trgm 유사도와 ILIKE뿐이고, substring 일치는 전부 1.0점이라 동점을 문서 id 순으로 가른다. `constraints.py:1105`와 [`query-types.md`](query-types.md)는 "BM25"라고 적고 있다 | 한국어 토크나이저(Kiwi, mecab-ko 등) 기반 BM25를 도입한다(`tsvector` 사용자 설정, `pg_bigm`, ParadeDB `pg_search` 중 택일). pg_trgm은 식별자·파일명 substring 층에만 남긴다. 문서의 BM25 표기를 바로잡는다 | term fallback의 최대 10회 조회와 df 쿼리가 사라져 오히려 감소 |
| S3 | RRF가 문서 단위로 합산되지 않고, 어휘 쪽은 term fallback으로 최대 10개 투표 리스트를 내는데 벡터는 1개다 | 소스 계열당 1표로 묶고 문서 id 기준으로 융합한 뒤 A/B를 다시 한다. 09-15의 문서 합의 보너스 기각은 투표 불균형이 그대로인 상태에서 잰 것이다 | 없음 |
| S4 ✔ | 원격 임베딩(OpenRouter `openai/text-embedding-3-small`, 1536차원)이다 | 온프렘 bge-m3 또는 Qwen3-Embedding을 `KF_EMBEDDING_BASE_URL`로 붙인다. `vector(N)` 마이그레이션과 재임베딩이 필요하다. 모델이 요구하면 query/passage 접두어를 넣을 자리를 만든다 | 약 −300ms, 결과가 결정론적이 되어 M4도 해결 |
| S5 | 규칙으로 뽑은 메타데이터가 하드 필터로 결과를 좁히거나 통째로 대체한다(`merge()`의 `no_overlap_fallback_to_filter`). "유통 채널 전략"은 여전히 slack 필터를 켜고, 파일명 질의는 `semantic_query`가 비어 벡터 arm이 돌지 않는다 | 규칙 메타데이터는 점수 부스트로 강등한다. 하드 필터는 명시적 구조 조건(따옴표, 확장자, 명시 날짜, 명시 인물)에만 쓴다. 보류 중인 `9aa9d8f`(파일명 semantic_query 복원)는 리랭커 도입 후 다시 잰다 | 없음 |
| S6 | 소스당 30개 단위만 가져오고, 절대 코사인 하한 0.45는 모델을 바꾸면 의미가 달라진다. 다양성(MMR) 처리가 없다 | 후보 풀을 넓히고 하한은 리랭커 점수로 옮긴다 | 리랭커 비용에 포함 |
| S7 | 메타데이터 필터 SQL을 문자열 보간으로 만든다(`_sql_literal`, `_like_literal`). LLM 보강값도 그대로 들어간다 | 파라미터 바인딩으로 바꾼다. 현재는 이스케이프와 READ ONLY 트랜잭션으로 영향이 제한된다 | 없음 |

## 5. 온톨로지 (지식 층)

유지할 장점은 provenance 사슬(source → document → chunk → knowledge object, `model_used`/`pipeline_version`/`source_hash`)과 `document_links`의 버전·스레드 관계다. 문제는 그 위의 객체 층이다.

| # | 문제 | 개선 |
|---|---|---|
| O1 | 엔티티 해소가 없다. 병합은 한 문서 안에서 summary 유사도로만 하고, 엔티티의 summary는 이름이 아니라 설명이라 거의 병합되지 않는다. `okf/default/objects/entity/`에 "정민찬"이 36개 파일로 있고 같은 문서에서 두 번 뽑힌 경우도 있다. 정규 엔티티 테이블이 없다 | `entities`(정규화 이름 + 타입) 테이블과 `entity_mentions`(엔티티 ↔ KO) 조인을 만든다. 이후 모든 항목의 전제다 |
| O2 | 타입별 속성이 없다. 9개 타입 중 8개의 `content`가 `{"statement": summary}` 하나다. `entity_type`·`document_type`은 LLM 자유 문자열이다. `models/knowledge_object.py`의 `Entity` 모델은 쓰이지 않는다 | decision(내용, 결정자, 날짜, 상태), action_item(담당, 기한, 상태), event(시점, 참여자)부터 실제 속성을 두고 저장 시 타입별 Pydantic 모델로 검증한다. `Entity` 모델은 이 작업에 흡수하거나 지운다 |
| O3 | relations를 읽는 코드가 검색·에이전트에 없다. 추출이 내는 관계 타입은 사실상 `related_to`/`mentions`/`caused_by` 셋이고 실제 술어는 자유 문자열 `label`에 있다 | 저장소에 `neighbors(object_id, depth, link_types)`를 추가하고 에이전트 도구와 `GET /relations/expand`로 노출한다. 관계 타입은 엔드포인트 타입 제약과 함께 좁힌다 |
| O4 | KO grain이 골든셋 v1의 5개 지표 전부에서 chunk grain보다 낮고 27% 느리다([`evaluation.md`](evaluation.md)). 그런데 `config.py`의 `embedding_source` 기본값은 여전히 `knowledge_object`다(로컬 `.env`는 `chunk`) ✔ | 기본값을 `chunk`로 바꾼다. KO grain은 O1·O2 이후 다시 잰다 |
| O5 | 추출 항목의 `confidence`를 받고도 버린다. KO에 confidence 컬럼이 없다. evidence는 청크 묶음 단위라 주장 하나가 최대 4000토큰을 근거로 가리킨다 | confidence를 저장하고, 가능하면 인용 구절이나 오프셋 단위 evidence를 남긴다. 향후 쓰기(action) 게이트의 신뢰 신호다 |
| O6 | 두 번째 소비 Agent가 쓸 인터페이스가 없다. MCP 서버가 없고, `list_knowledge_objects`에 `project_id` 필터가 없어 다른 소비자가 프로젝트 경계를 넘어 읽을 수 있다 | `lookup_entity`, `get_object`, `neighbors`, `search` 네 개의 읽기 전용 MCP 서버를 만든다. 모든 도구에 `project_id` 범위를 강제한다. O1·O3의 수용 시험 역할도 한다 |
| O7 | Qwen3.8-27B 같은 오픈소스 모델로 추출하려면 strict `json_schema`가 필요한데, `_to_strict_json_schema`가 `default`·`title` 키를 남긴다. 실제 라우팅 모델로 검증된 적이 없다 | 두 키를 제거하고, 대상 서빙 백엔드에서 strict 모드 지원을 확인한다. 미지원이면 JSON 파싱 재시도 경로를 둔다 |

## 추천 순서

1. **측정 기반 확보** — M1, M3, M4. 이것 없이는 아래 변경의 품질·지연 효과를 판단할 수 없다.
2. **품질 영향 없는 지연 절감과 오픈소스 대응** — L1, L3, L5, R1~R4. 변경 범위가 어댑터·루프·도구 정의 안에 머문다.
3. **검색 부품 교체** — S4 → S1 → S2 → S3·S5. 임베딩을 먼저 바꿔야 이후 A/B가 흔들리지 않는다. 리랭커가 들어오면 S5·S6의 규칙을 걷어낼 수 있다.
4. **측정 후 결정** — L2, L4, M5.
5. **온톨로지 층** — O4는 즉시, 이후 O1 → O2 → O3 → O6 순서. O1이 없으면 O2·O3이 붙을 안정 id가 없다. 3단계와 독립이라 병렬로 진행할 수 있다.

## 관련 문서

- [`agent-runtime.md`](agent-runtime.md), [`agent-search.md`](agent-search.md) — 에이전트 도구 표면과 루프
- [`search-architecture.md`](search-architecture.md), [`search-quality-review-2026-09-15.md`](search-quality-review-2026-09-15.md) — 검색 코어와 기존 결함 목록
- [`evaluation.md`](evaluation.md) — 골든셋과 e2e 평가
- [`knowledge-object-schema.md`](knowledge-object-schema.md), [`roadmap.md`](roadmap.md) — 객체 스키마와 범위
