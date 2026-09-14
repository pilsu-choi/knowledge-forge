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
| **라벨 확장**: 같은 grain `retrieve()` 상위 8개 후보를 LLM이 "같은 답을 담는가" 이진 판정, 담으면 정답에 추가 | `positive_without_quote`, `anchorless`(정답 5개 초과) | 다른 문서에도 같은 답이 있는데 한 곳만 정답으로 둬서 맞힌 검색을 틀렸다고 채점 |
| 같은 content_hash 문서의 동일 인용문은 판정 없이 추가 | - | 복제 수집본 |
| exhaustive 정답은 LLM이 아니라 SQL 술어(구글 드라이브 확장자, 슬랙 채널, 소스+월(Asia/Seoul), 소스), 문서 2~50건 | - | LLM이 지어낸 "전부" 정답 |
| none: 핵심 term 문서빈도 0 **그리고** 같은 grain 벡터 최고 유사도 < `KF_VECTOR_MIN_SCORE` | `none_term_in_corpus`, `none_vector_too_similar` | 실제로는 코퍼스에 있는 "없는 주제" |

검색이 원래 정답을 못 찾아도 항목은 버리지 않는다. 그게 평가가 잡아야 할 실패다.

샘플링: chunk grain은 `token_count >= 80` 청크, KO grain은 entity가 아니고 대체(supersedes)되지 않은 KO(summary·content와
evidence 청크 원문을 함께 보여줌). 둘 다 시드 고정 셔플 → source_type 라운드로빈 → 문서당 2개.
구성 기본 300 = lookup 210 / exhaustive 45 / none 45(`--n`에 비례), kind마다 dev/test 번갈아. 산출은 `status: draft`이고,
사람이 20~30개를 훑고 `approved`로 바꾼다.

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

## 지표

| kind | 지표 | 정의 |
|---|---|---|
| lookup | hit@1 | 1위가 정답이면 1 |
| | recall@5 | 상위 5개에 든 정답 수 / 정답 수 |
| | mrr | 첫 정답 순위의 역수 |
| | ndcg@10 | 이진 관련도, IDCG는 min(정답 수, 10)개 기준 |
| exhaustive | recall, precision | 채점 리스트를 문서로 올린 집합 vs 술어 결과(컷오프 없음) |
| none | none_correct | 채점 리스트가 비었으면 1 |
| 공통 | score | 대표 지표: lookup ndcg@10 / exhaustive recall / none none_correct |
| e2e | answer_correct, declined, latency_ms, llm_calls, tool_calls | judge `{"verdict": correct/partial/wrong, "declined": bool}`. none 항목은 declined가 곧 정답 |

요약은 kind별 평균과 n, overall(n, stale, error, 모든 항목에 있는 지표 평균).
e2e 채점 리스트는 search 도구 결과를 **호출 순서**(디스패치 시점에 자리를 잡음, 동시 도구 호출의 완료 순서가 아님)로 이어 붙인 것이다.

compare는 두 실행 모두 ok인 항목끼리 짝지어 delta(B−A) 평균, 시드 고정 bootstrap 95% CI(1만 회), 승/패/무를 낸다.
골든셋 버전이나 문서 지문이 다르면 거부하고, 청크·KO·임베딩 지문만 다르면 경고한다(재청킹·임베딩 교체 A/B가 그 경우).
n=300이어도 +0.03 이하 차이는 CI가 0을 포함할 수 있다.

## 알려진 한계

- **chunk grain 점수는 문서 단위 히트에 구조적으로 눌린다.** 2026-09-14 스모크(lookup 7개)에서 5개는 채점 리스트가 비었다:
  `metadata_filter`나 `document_lookup` anchor가 걸리면 retrieve 결과가 청크 id 없는 문서 히트 위주가 된다. 정답 문서를 1위로
  찾고도 정답 청크가 리스트에 없어 0점인 항목도 있었다. 계획대로의 채점 규칙이며, 개선 여부는 S2에서 판단할 일이다.
- **KO grain은 사실상 벡터 단독**이다. KO 어휘 검색 arm이 없어 lexical/keyword 히트는 전부 청크라 채점 리스트에서 빠진다.
- KO grain은 KO 임베딩이 먼저 있어야 한다: `knowledge-forge embed --embedding-source knowledge_object`. 0건이면 `eval gen`이
  거부하고 `eval run`은 경고를 남긴다. 개발 DB는 2026-09-14에 5,107건을 백필했다(chunk 2,856 / knowledge_object 5,107, `openai/text-embedding-3-small`).
- `OPENROUTER_API_KEY`는 셸에 export해야 한다. settings는 `.env`의 `KF_` 접두사 키만 읽는다.
- none 게이트의 term 문서빈도는 청크 본문 ILIKE 기준(`retrieve.py::_term_document_frequency_sql` 재사용)이라 제목에만 있는 단어는 못 본다.
- e2e의 `delegate_task` 자식 에이전트는 grain 없이 검색하고, 그 검색 결과는 채점 리스트에 들어가지 않는다.
- 합성 질의는 여전히 "메일 테스트" 같은 빈약한 원문에서 약한 질의를 만든다. 사람 스팟체크가 필요하다.

## 골든셋 v1과 기준 점수 (2026-09-14)

`eval gen --n 300 --seed 20260914`, `openai/gpt-4o-mini`로 두 벌을 만들었다. 둘 다 281개다.

| 골든셋 | lookup | exhaustive | none | 정답 문서 수(lookup) |
|---|---|---|---|---|
| `chunk-v1.json` | 210 | 26 | 45 | 190개 문서, 문서당 최대 2 |
| `knowledge_object-v1.json` | 210 | 26 | 45 | — |

- exhaustive가 목표 45에 못 미친 이유: 2~50문서 크기의 SQL 술어 후보가 26개뿐이었다. 코퍼스가 커지면 다시 생성한다.
- 거부 사유 상위: none 항목의 term이 코퍼스에 있음(chunk 102 / KO 142), none 벡터 유사도 초과(40 / 68), 원시 id(KO 44), 앵커 없음(chunk 18).

main 코드(`7160f0d`) 기준점, 전체 split, LLM 0회:

| 골든셋 | overall | lookup ndcg@10 | hit@1 | recall@5 | exhaustive recall | none_correct | 질의당 p50 |
|---|---|---|---|---|---|---|---|
| chunk-v1 | 0.422 | 0.474 | 0.381 | 0.502 | 0.042 | 0.400 | 1.1초 |
| knowledge_object-v1 | 0.388 | 0.446 | 0.314 | 0.463 | 0.013 | 0.333 | 1.4초 |

- lookup 채점 리스트가 빈 항목이 chunk 31건, KO 30건이다. 제목·필터 히트는 문서 단위라 grain id가 없어서다(검색 개선 S2의 "grain 펼치기"가 겨냥).
- exhaustive recall이 거의 0인 것도 같은 원인이다. 필터가 찾은 문서가 grain 채점 리스트에 들어가지 않는다.
- none_correct가 낮은 이유: 채점 리스트가 0건이어야 정답인데 어휘 arm이 무언가를 물어온다.
