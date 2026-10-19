# 검색 평가 (eval gen / run / compare)

2026-09-14 재구현. 코드 `src/knowledge_forge/eval/`(모듈 4개), CLI `knowledge-forge eval`(하위 명령 3개), DB 테이블 없음.

## 왜 다시 만들었나

이전 평가(eval-gen/eval-ir, eval_run 테이블, 22개 질의 유형)는 3,000줄 넘게 커졌고, 합성 골든셋 품질이 결과를 흔들었다.
원문 복사 질의, 예/아니오 질의, 어느 문서인지 특정되지 않는 질의, 청크 id 라벨의 재청킹 무효화, dirty 트리를 못 잡는
provenance가 반복해서 문제였다. 이번에는 **실패 유형마다 자동 게이트 하나**를 두고, 나머지는 최소로 줄였다.

- 기본은 retrieval만 잰다(LLM 0회, 결정론). 에이전트 답변까지 보는 e2e는 `--e2e` 옵션.
- 골든셋은 파일(`tests/eval/golden/<version>.json`), 실행 결과도 파일(`out/eval/<run_id>.json`, gitignore).

## grain

골든셋은 생성할 때 기준 단위 `chunk` 또는 `knowledge_object`를 정하고, **평가는 그 grain으로만 검색하고 채점한다.**
run에는 grain 인자가 없고 골든셋 파일의 값을 따른다.

- 검색: `retrieve(..., grain=...)`가 모든 벡터 조회(vector arm, multihop planner)를 그 grain 임베딩으로 좁힌다.
  `KF_EMBEDDING_SOURCE`와 무관하게 grain이 이긴다. `grain=None`(기본)은 `KF_EMBEDDING_SOURCE`의 grain 하나만 본다. 2026-09-14 KO 임베딩을 백필해 두 grain이 같이 적재되자, 섞어 보면 같은 문서가 청크와 KO로 두 번 오르므로 기본값을 설정 grain으로 고정했다.
- 채점 리스트: 히트 중 그 grain의 id(`chunk_id` / `knowledge_object_id`)를 가진 것만 순서대로(중복 제거).
  제목·필터 히트는 순위 결정에는 참여하지만 grain id가 없어 채점 리스트에서 빠진다.
- 정답 라벨
  - chunk lookup: `evidence: [{document_id, quote}]`. 청크 id가 아니라 **인용문**이라 실행 시점에 "그 문서에서 quote를
    담은 청크"(NFC + 공백 접기 후 포함)로 해석된다. 재청킹 뒤에도 골든셋이 살아 있다. 담은 청크가 없으면 stale.
  - KO lookup: `knowledge_object_ids`. 재추출로 id가 없어지면 stale.
  - exhaustive: `predicate_sql` + 생성 당시 `document_ids`. 실행 때 술어를 다시 돌려 집합이 바뀌었으면 stale.
    이 kind만 grain 히트를 소속 문서 id로 올려 채점한다.
  - none: 라벨 없음. 채점 리스트가 비어야 정답.

## 생성 게이트와 막는 실패

| 게이트 | 거부 사유(`rejects`) | 막는 과거 실패 |
|---|---|---|
| 원문과 최장 공통 부분문자열 ≥ 25자 | `copies_source` | 원문 문장을 베낀 질의가 어휘 검색만으로 풀려 점수를 부풀림 |
| 예/아니오 질의(의문사 없이 "~나요?/~습니까?", "여부") | `yes_no` | 정답 문서가 사실상 정해지지 않는 질의 |
| 원시 id(슬랙 채널 id `C0BT6M412N9`류, 16자 이상 hex) | `raw_id` | 사람이 칠 수 없는 식별자로 lexical 경로만 푸는 질의 |
| 12자 미만 | `too_short` | 앵커 없는 짧은 질의 |
| 이 문서/해당 프로젝트 같은 지시어 | `deictic` | 원문을 본 사람만 아는 "이 프로젝트" 질의 |
| 인용문이 원문에 없음(구두점만 바뀐 경우 원문 구절로 고정) | `quote_not_in_source` | 해석 불가능한 라벨 |
| **라벨 확장**: `retrieve()`(전체 파이프라인) + `vector_search`(벡터 단독) + chunk grain이면 `lexical_search`(어휘 단독), 세 arm 상위를 라운드로빈으로 섞어 최대 8개 후보를 LLM이 "같은 답을 담는가" 이진 판정, 담으면 정답에 추가 | `positive_without_quote`, `anchorless`(정답 5개 초과) | 다른 문서에도 같은 답이 있는데 한 곳만 정답으로 둬서 맞힌 검색을 틀렸다고 채점 |
| 같은 content_hash 문서의 동일 인용문은 판정 없이 추가 | - | 복제 수집본 |
| exhaustive 정답은 LLM이 아니라 SQL 술어(구글 드라이브 확장자, 슬랙 채널, 소스+월(Asia/Seoul), 소스), 문서 2~50건 | - | LLM이 지어낸 "전부" 정답 |
| none: 핵심 term 문서빈도 0 **그리고** 같은 grain 벡터 최고 유사도 < `KF_VECTOR_MIN_SCORE` | `none_term_in_corpus`, `none_vector_too_similar` | 실제로는 코퍼스에 있는 "없는 주제" |

검색이 원래 정답을 못 찾아도 항목은 버리지 않는다. 그게 평가가 잡아야 할 실패다.

**라벨 확장 풀링 편향(2026-09-15 수정)**: 후보를 `retrieve()` 상위 8개에서만 뽑으면, 그 파이프라인이
놓친 정답 청크는 다른 arm(순수 벡터·순수 어휘)이 찾아도 영영 오답으로 채점된다 - A/B가 라벨을 만들 때
쓴 파이프라인 쪽에 구조적으로 유리해진다(`wiki/search-quality-review-2026-09-15.md` 3.1). 지금은
세 arm의 상위를 `generate.py::pool_candidate_ids`로 라운드로빈 병합·중복 제거한 뒤 같은 판정 함수
(`_judge_candidates`)로 넘긴다 - 총 후보 수(`EXPANSION_TOP`=8)는 그대로라 LLM 비용은 비슷하다. KO
grain은 `lexical_search`가 청크 히트만 내놓아 채점에 참여하지 못하므로 그 arm을 아예 건너뛴다.

샘플링: chunk grain은 `token_count >= 80` 청크, KO grain은 entity가 아니고 대체(supersedes)되지 않은 KO(summary·content와
evidence 청크 원문을 함께 보여줌). 둘 다 시드 고정 셔플 → source_type 라운드로빈 → 문서당 2개.
구성 기본 300 = lookup 210 / exhaustive 45 / none 45(`--n`에 비례). 산출은 `status: draft`이고,
사람이 20~30개를 훑고 `approved`로 바꾼다.

**dev/test 분할(2026-09-15 수정)**: `generate.py::split_key`(lookup은 `document_id`, exhaustive는 정답
문서 중 가장 앞선 id, none은 질의 자체) → `split_for`(sha256 첫 바이트의 홀짝)로 정한다. 문서당 최대 2개인
lookup이 항상 같은 쪽에 떨어져, dev로 튜닝한 결과가 test로 새지 않는다. 이전에는 kind별 생성 순서의 홀짝
(`i % 2`)으로 나눠 같은 문서의 lookup 두 개가 dev/test에 걸쳐 새는 문제가 있었다
(`wiki/search-quality-review-2026-09-15.md` 3.4). **v1(`chunk-v1.json`/`knowledge_object-v1.json`)은
옛 홀짝 분할 그대로다** - 다시 쓰지 않았다. 두 파일 모두 `split`이 항목에 이미 박혀 있어 필드 자체에서
재계산할 수는 있지만(각 항목의 `document_id`/`evidence[0].document_id`/`document_ids`/`query`에
`split_for(split_key(item))`을 다시 적용), 과거 run과의 비교가 깨지므로 적용하지 않았다. 다음에 `eval gen`으로
새로 만드는 골든셋부터 새 분할을 쓴다.

## 실행 방법

```bash
# .env의 KF_ 접두사 없는 키는 settings가 읽지 않는다 - export 필요
export OPENROUTER_API_KEY=$(grep '^OPENROUTER_API_KEY=' .env | cut -d= -f2-)

knowledge-forge eval gen --grain chunk            # tests/eval/golden/chunk-<시각>.json
knowledge-forge eval gen --grain knowledge_object --n 300 --seed 0 --model openai/gpt-4o-mini
knowledge-forge eval run --golden tests/eval/golden/<version>.json [--split dev]   # LLM 0회
knowledge-forge eval run --golden ... --e2e --concurrency 4                        # 에이전트 + judge
knowledge-forge eval run --golden ... --llm-constraints                            # 제약 LLM 보강 + multihop 켬
knowledge-forge eval compare out/eval/<A>.json out/eval/<B>.json [--metric ndcg@10]
```

결과 JSON에는 항목별 정답 id·채점 리스트·지표·retrieve steps, 실행 전후 코퍼스 지문, provenance가 들어간다.
provenance는 git 커밋(더러우면 `-dirty.<sha256(git diff HEAD + 추적 안 된 파일)>`), 랭킹 설정(`vector_min_score`,
`term_coverage_ratio`, `term_coverage_min`, `term_rare_df_max`, `latest_relevance_pool`, `embedding_model/source/provider`),
모델명이다. 코퍼스 지문은 문서(id:content_hash:title 해시), 청크 id 해시, KO id 해시, 활성 임베딩 모델의 grain별 임베딩 수다.

골든셋 자체의 `corpus`에도 생성 시점 값 하나가 더 있다: `corpus.none_gate.vector_min_score` - none 항목을
채택할 때 쓴 `KF_VECTOR_MIN_SCORE`(2026-09-15 추가, `dict[str, Any]`라 이 키가 없는 v1 파일도 그대로
로드된다). none 정답 여부(`run.py::score_item`)는 채점 리스트가 비었는지만 보고 이 값을 다시 보지 않지만,
게이트 자체가 이 값에 묶여 있어서 하한을 올리기만 해도 none_correct가 오른다 - 검색 품질이 아니라 그
때 쓴 임계값을 기록해 두는 것으로, 근본 해결은 아니다(`wiki/search-quality-review-2026-09-15.md` 3.2).

## 지표

| kind | 지표 | 정의 |
|---|---|---|
| lookup | hit@1 | 1위가 정답이면 1 |
| | recall@5 | 상위 5개에 든 정답 수 / 정답 수 |
| | mrr | 첫 정답 순위의 역수 |
| | ndcg@10 | 이진 관련도, IDCG는 min(정답 수, 10)개 기준 |
| exhaustive | recall, precision | 채점 리스트를 문서로 올린 집합 vs 술어 결과(컷오프 없음) |
| none | none_correct | 채점 리스트가 비었으면 1 |
| 항목별 | score | 대표 지표: lookup ndcg@10 / exhaustive recall / none none_correct |
| e2e | answer_correct, declined, latency_ms, llm_calls, tool_calls | judge `{"verdict": correct/partial/wrong, "declined": bool}`. none 항목은 declined가 곧 정답 |

요약(`run.py::summarize`)은 kind별 평균 + n, 그리고 `overall`은 **n/stale/error만**이다(2026-09-15 수정).
이전에는 `overall`에도 모든 항목에 있는 지표(사실상 `score`뿐)의 평균을 넣었는데, 그건 lookup ndcg@10 /
exhaustive recall / none none_correct라는 서로 다른 척도를 평균 낸 것이라 골든셋의 kind 비율(210/45/45)이
바뀌면 검색 품질과 무관하게 값이 움직인다(`wiki/search-quality-review-2026-09-15.md` 3.3). 비교는 항상
kind별 표로 본다 - `knowledge-forge eval run`은 이미 `summary`의 각 키(kind마다 + overall)를 한 줄씩 찍는다.
e2e 채점 리스트는 search 도구 결과를 **호출 순서**(디스패치 시점에 자리를 잡음, 동시 도구 호출의 완료 순서가 아님)로 이어 붙인 것이다.

compare는 두 실행 모두 ok인 항목끼리 짝지어 delta(B−A) 평균, 시드 고정 bootstrap 95% CI(1만 회), 승/패/무를 낸다.
골든셋 버전이나 문서 지문이 다르면 거부하고, 청크·KO·임베딩 지문만 다르면 경고한다(재청킹·임베딩 교체 A/B가 그 경우).

**재현성 주의(2026-09-15 실측).** 질의 임베딩은 실행마다 원격(OpenRouter)으로 새로 받는데, 같은 문장에도 벡터가 조금씩 달라진다.
같은 main 코드를 두 번 돌려도 골든 562질의 중 137건(chunk 58 / KO 79)의 retrieve 결과가 달랐다(대부분 점수 소수점 5자리, 일부는 순위).
질의 임베딩을 한 번 기록해 재생하면 같은 코드끼리 562/562 동일했고, 알려진 흔들림 항목 KO `lookup-006`도 멈췄다.
그래서 작은 delta(±0.01대)의 A/B는 임베딩 노이즈와 섞인다 - 판정이 애매하면 질의 임베딩을 고정해 다시 잴 것.
고정 재생 스크립트는 아직 `eval run`에 들어가지 않았다(검증에 쓴 것은 세션 scratchpad의 `pinned.py`, 휘발).
**두 run의 `provenance.settings`가 다르면(`vector_min_score` 포함) 경고한다(2026-09-15 추가)** - none 게이트와
채점이 둘 다 그 값에 기대므로, 그럴 때 none_correct(그리고 다른 지표) delta는 검색 품질이 아니라 설정 차이를
잰 것일 수 있다. n=300이어도 +0.03 이하 차이는 CI가 0을 포함할 수 있다.

## 2026-09-15 평가 편향 수정

`wiki/search-quality-review-2026-09-15.md` 3장("평가")이 지적한 네 가지. 코드는 `src/knowledge_forge/eval/`,
테스트는 `tests/eval/`(golden JSON 제외).

1. **라벨 확장 풀링 편향** - `generate.py::expand_labels`가 `retrieve()` 상위 8개에서만 후보를 뽑아, 그
   파이프라인이 놓친 정답이 영구히 오답으로 채점됐다. `_candidate_pool`이 `retrieve()` + `vector_search`
   + (chunk grain만) `lexical_search` 세 arm을 `pool_candidate_ids`로 라운드로빈 병합해 판정한다.
2. **none 게이트가 튜닝 값에 묶임** - 생성 게이트(`_Generator.none`)는 `KF_VECTOR_MIN_SCORE`로 채택 여부를
   정하고 채점(`run.py::score_item`)은 "채점 리스트가 비었나"만 본다. 근본 해결은 아니지만, 골든셋
   `corpus.none_gate.vector_min_score`에 생성 시 값을 남기고, `eval compare`가 두 run의
   `provenance.settings`(특히 이 값)가 다르면 경고하게 했다 - none_correct delta가 임계값 차이 때문일
   수 있음을 표시한다.
3. **overall이 이종 지표를 평균** - `run.py::summarize`의 `overall`이 lookup ndcg@10 / exhaustive recall /
   none none_correct를 전부 `score`로 묶어 평균 냈다. `overall`은 이제 n/stale/error만 담고, 지표는
   kind별 표로만 본다(CLI가 이미 `summary`의 각 키를 한 줄씩 찍는다).
4. **dev/test 번호 짝홀 누수** - 문서당 lookup이 최대 2개인데 생성 순서 홀짝으로 나누면 같은 문서가
   dev/test 양쪽에 들어갈 수 있었다. `split_key`(lookup: `document_id`, exhaustive: 정답 문서 중 최소 id,
   none: 질의) + `split_for`(sha256 홀짝)로 문서 단위 안정 해시 분할로 바꿨다. **`chunk-v1.json`/
   `knowledge_object-v1.json`은 여전히 옛 분할**이다(다시 쓰지 않음 - 과거 run과의 비교 보존).

## 알려진 한계

- **grain id는 검색 경로마다 있거나 없다 - 채점은 id가 있는 히트만 센다(`goldenset.py::ranked_ids`).**
  `document_search`(제목)와 `metadata_filter`는 `documents`/`sources` 행을 조회해 문서 단위 히트만 내고
  (chunk_id·KO id 둘 다 없음), `lexical_search`는 `chunks` 행이라 chunk_id는 있지만 `knowledge_object_id`는
  항상 None이다(`postgres_store.py::search_chunks_lexical`). KO id를 내는 경로는 `vector_search`뿐이다.
  그래서 grain 펼치기 전(기준점 `7160f0d`, 아래 v1 표)에는 정답 문서를 1위로 찾고도 채점 리스트가 비었다 -
  lookup 빈 리스트 31/30건, exhaustive recall 0.042/0.013. 예: chunk `exhaustive-005`("2026년 8월에 발송된
  메일을 전부")는 필터가 정답 48건을 정확히 찾았지만 벡터 200건이 전부 하한 미만으로 버려져
  `no_overlap_fallback_to_filter`로 문서 히트만 남아 recall 0이었다.
- **grain 펼치기(`retrieve.py::_Run.expand_to_grain`, 2026-09-14 채택)가 이걸 뒤에서 메운다.** 최종 랭킹에서
  grain 히트가 없는 문서마다 그 문서 안에서만 `vector_search(document_ids=[문서])`로 grain 단위 3개를 골라
  문서의 첫 히트 뒤에 끼운다. 효과: test split chunk exhaustive recall 0.049 → 0.805, 종합 chunk +0.123 / KO
  +0.046(`search-architecture.md` "랭킹 구조 수정 A/B"). 남은 한계:
  - **KO grain에서 "어느 문서인가"는 모든 arm이, "문서 안의 어느 KO인가"는 벡터만 정한다.** KO 어휘 검색이
    없어서다. chunk grain은 어휘 arm의 청크 히트가 그대로 채점된다.
  - 문서당 3개씩 끼우는 단위가 다른 정답을 밀어낸다 - test KO lookup ndcg@10 −0.010(4승 13패). 끼우는 개수·
    유사도 하한 조정이 후속 과제다.
  - 문서마다 `vector_search`가 1회씩 붙고 wide 질의는 절단하지 않아 조회가 불어난다
    (`search-quality-review-2026-09-15.md` 2장 9번).
  - `grain`을 명시할 때(평가·e2e)만 켜진다. 에이전트 `search` 도구 기본값(grain 미지정)은 펼치지 않으므로
    펼치기 이득은 제품 경로에 반영되지 않는다.
- KO grain은 KO 임베딩이 먼저 있어야 한다: `knowledge-forge embed --embedding-source knowledge_object`. 0건이면 `eval gen`이
  거부하고 `eval run`은 경고를 남긴다. 개발 DB는 2026-09-14에 5,107건을 백필했다(chunk 2,856 / knowledge_object 5,107, `openai/text-embedding-3-small`).
- `OPENROUTER_API_KEY`는 셸에 export해야 한다. settings는 `.env`의 `KF_` 접두사 키만 읽는다.
- none 게이트의 term 문서빈도는 청크 본문 ILIKE 기준(`retrieve.py::_term_document_frequency_sql` 재사용)이라 제목에만 있는 단어는 못 본다.
- e2e의 `delegate_task` 자식 에이전트는 grain 없이 검색하고, 그 검색 결과는 채점 리스트에 들어가지 않는다.
- 합성 질의는 여전히 "메일 테스트" 같은 빈약한 원문에서 약한 질의를 만든다. 사람 스팟체크가 필요하다.
- 2026-09-15 품질 조사([`search-quality-review-2026-09-15.md`](search-quality-review-2026-09-15.md) "3. 평가")가 짚은
  라벨 풀링 편향, none의 `vector_min_score` 결합, overall 이종 지표 평균, 짝홀 dev/test 누수는 아래 "2026-09-15 평가 편향 수정"에서 고쳤다.
- **스코프 밖(다음 세션)**: judge 보정(사람 라벨과의 일치율을 잰 적이 없음), exhaustive stale 판정이
  너무 엄격함(술어 결과가 문서 하나만 달라져도 항목 전체가 stale), exhaustive 질의 자연스러움(검색
  품질보다 `constraints` 규칙 적중을 재는 경향), grain id 없는 히트가 0점 처리되는 것 자체(위 첫 항목 -
  채점 규칙을 바꾸는 건 이번 수정 범위가 아니다).

## 골든셋 v1과 기준 점수 (2026-09-14)

`eval gen --n 300 --seed 20260914`, `openai/gpt-4o-mini`로 두 벌을 만들었다. 둘 다 281개다.

| 골든셋 | lookup | exhaustive | none | 정답 문서 수(lookup) |
|---|---|---|---|---|
| `chunk-v1.json` | 210 | 26 | 45 | 190개 문서, 문서당 최대 2 |
| `knowledge_object-v1.json` | 210 | 26 | 45 | — |

- exhaustive가 목표 45에 못 미친 이유: 2~50문서 크기의 SQL 술어 후보가 26개뿐이었다. 코퍼스가 커지면 다시 생성한다.
- 거부 사유 상위: none 항목의 term이 코퍼스에 있음(chunk 102 / KO 142), none 벡터 유사도 초과(40 / 68), 원시 id(KO 44), 앵커 없음(chunk 18).

main 코드(`7160f0d`) 기준점, 전체 split, LLM 0회. **`overall` 열은 그 시점의 정의(이종 지표 평균, 위
"2026-09-15 평가 편향 수정" 3번 참고)로 낸 값이라 지금의 `summarize()`는 이 열을 더 이상 내지 않는다 -
당시 기록 그대로 남겨 둔다.**

| 골든셋 | overall(구 정의) | lookup ndcg@10 | hit@1 | recall@5 | exhaustive recall | none_correct | 질의당 p50 |
|---|---|---|---|---|---|---|---|
| chunk-v1 | 0.422 | 0.474 | 0.381 | 0.502 | 0.042 | 0.400 | 1.1초 |
| knowledge_object-v1 | 0.388 | 0.446 | 0.314 | 0.463 | 0.013 | 0.333 | 1.4초 |

- **이 표는 grain 펼치기 이전 값이다.** 펼치기는 이 기준점 뒤에 main에 머지됐다(`37c42cb`).
- lookup 채점 리스트가 빈 항목이 chunk 31건, KO 30건이다. 제목·필터 히트는 문서 단위라 grain id가 없어서다(위 "알려진 한계" 첫 두 항목, grain 펼치기가 겨냥).
- exhaustive recall이 거의 0인 것도 같은 원인이다. 필터가 찾은 문서가 grain 채점 리스트에 들어가지 않았다. 펼치기 후 test chunk 0.049 → 0.805.
- none_correct가 낮은 이유: 채점 리스트가 0건이어야 정답인데 어휘 arm이 무언가를 물어온다.
