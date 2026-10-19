---
type: Design Note
title: 검색 코어 v2 - 융합 단위·메타데이터 강등·한국어 BM25·리랭커
description: 개선안 S3·S5·S2·S1을 설정 플래그 뒤에 구현한 내용. 기본값은 전부 off이고 A/B는 골든셋 재생성 후 한다.
tags: [search, rrf, bm25, reranker, flags]
status: draft
generated:
  by: claude-opus-5.5
  at: 2026-10-19
sources:
  - resource: improvement-plan-2026-09-15.md
    title: 개선 필요 사항 (S1, S2, S3, S5)
---

# 검색 코어 v2 (2026-10-19)

[`improvement-plan-2026-09-15.md`](improvement-plan-2026-09-15.md) 4장의 S3·S5·S2·S1을 구현했다.
**모든 플래그의 기본값은 off**이고, off일 때 결과는 기존과 같다(기존 테스트가 보장). 켠 실행은
`steps`에 새 단계가 남는다. 효과는 골든셋을 다시 만든 뒤 A/B로 잰다 - 이 문서의 숫자는 없다.

| 플래그 | 기본 | 단계(`steps.strategy`) |
|---|---|---|
| `KF_SEARCH_ARM_FUSION` | false | `arm_fusion` |
| `KF_SEARCH_METADATA_SOFT_FILTER` | false | `metadata_boost` |
| `KF_SEARCH_BM25` | false | `lexical_search#semantic`의 `ranker` |

## S3: RRF 융합 단위

기존 `merge()`는 소스 리스트마다 한 표를 준다. 어휘 arm은 term fallback이 `lexical_search#<term>`을 최대
10개 내므로 어휘가 최대 10표(+제목 `#semantic`, 본문 `#semantic`), 벡터는 1표다. 켜면
(`rerank.merge_by_arm`):

1. 소스 이름의 `#` 앞을 계열(arm)로 본다(`document_search`, `lexical_search`, `vector_search`...).
2. 계열 안의 리스트들은 문서 id 기준 RRF로 한 순위표가 된다. 여러 term에 걸린 문서가 위로 온다는
   term fallback의 의도는 유지된다.
3. 계열 순위표가 바깥 RRF의 한 표다. 청크/KO 단위 히트는 문서 하나로 접힌다(먼저 나온 히트가 대표).

`retrieval_sources`는 계열 이름이 된다. 09-15의 "문서 합의 보너스 기각"은 투표 불균형이 남은 채로 잰
것이라, 이 플래그로 다시 잴 수 있다.

## S5: 메타데이터 하드 필터 강등

기존에는 규칙이 뽑은 `source_type` 힌트가 WHERE에 들어가 교집합으로 후보를 자르거나(`intersected`),
교집합이 0이면 필터 집합으로 통째로 대체했다(`no_overlap_fallback_to_filter`). 켜면
(`KF_SEARCH_METADATA_SOFT_FILTER`) 둘로 나눈다.

- **하드**: 질의가 명시한 구조 조건 - 작성자/참여자, 시간 범위, 파일 확장자·MIME. 동작은 기존과 같다
  (교집합, 교집합 0이면 필터 집합). 이때 WHERE에서 `source_type` 술어는 빠진다.
- **소프트**: `source_type` 힌트(`document_types`의 "메일" 매핑 포함). 힌트만 따로 한 번 조회해 얻은 문서
  집합을, **이미 후보인** 문서에 한해 RRF 한 표로 얹는다(`link_cohesion`과 같은 방식). 후보를 자르거나
  새로 데려오지 않는다. `steps`에 `metadata_boost`(올라간 문서 수 `promoted`)가 남고
  `metadata_filter.applied`는 `source_hint_boosted`다. 후보가 아예 없고 힌트뿐이면 힌트 집합이 답이다
  (`source_hint_only`, `filter_only`와 이름을 나눈 이유는 `count()`가 그 이름으로 SQL COUNT를 고르기 때문).

따옴표 구절·식별자는 metadata_filter가 아니라 `lexical` 전략이 이미 다루므로 여기서 하드 필터가 아니다.
이 플래그는 "파일명 질의는 `semantic_query`가 비어 벡터 arm이 안 돈다"를 고치지 않는다(보류 중인
`9aa9d8f`는 리랭커 뒤 재측정).

## S2: 한국어 BM25

현재 어휘 검색(`search_chunks_lexical`)은 pg_trgm이다. substring 일치는 전부 1.0점이고 동점은 문서 id 순이며,
조사 변형("전략을"/"전략은")을 같은 단어로 보지 못한다. `constraints.py`와 `query-types.md`가 이걸 "BM25"라고
적고 있어 바로잡았다.

| 옵션 | 장점 | 비용 |
|---|---|---|
| (a) ParadeDB `pg_search` | DB 안에서 진짜 BM25, 한국어 토크나이저(lindera ko-dic), 색인 자동 갱신 | DB 이미지를 `pgvector/pgvector`에서 ParadeDB로 교체(데이터 디렉터리 이전/재적재), `schema.sql`에 확장 의존 분기, AGPL 라이선스 검토, 개발·테스트·운영 DB를 모두 바꿔야 함 |
| **(b) 앱 쪽 Kiwi + SQL BM25** (채택) | DB 이미지 그대로, 의존성은 `korean` extra의 `kiwipiepy` 하나, 토크나이저·BM25 파라미터를 우리가 통제 | 청크 저장 때 색인을 우리가 갱신(한 트랜잭션), 기존 청크는 백필 필요, 질의 때 N·평균 길이·df를 SQL로 계산 |
| (c) `tsvector` + `ts_rank` | 내장 | `ts_rank`는 IDF/문서 길이 정규화가 없어 BM25가 아니다. 한국어 사전도 없다 |

(a)가 장기적으로 가장 단순한 형태지만 운영 DB 교체가 선행 조건이라 지금 넣기엔 무겁고, 이 환경에서
실제 컨테이너 시험은 하지 않았다. (b)는 플래그가 꺼져 있으면 스키마에 빈 테이블 하나와 컬럼 하나가 더
있을 뿐이고, 이미지·의존성(extra)이 그대로라 되돌리기 쉽다. 나중에 (a)로 가도 `lexical_search(bm25=True)`
뒤의 `search_chunks_bm25` 한 메서드만 바꾸면 된다.

**구현** (`KF_SEARCH_BM25`, `korean_tokens.py`, `PostgresStore.search_chunks_bm25`):

- 색인: `chunk_terms(term, chunk_id, tf)`(PK가 term 선두라 posting 조회와 df가 인덱스 범위 스캔) +
  `chunks.lex_len`(term 수, NULL=미색인). term은 Kiwi 형태소 중 일반/고유명사·수사·어근·용언 어간·외국어·숫자·한자를
  소문자로 남긴 것이다(조사·어미·의존명사 제외).
- 갱신: 플래그가 켜져 있으면 `save_chunks`가 같은 트랜잭션에서 색인한다(토큰화는 트랜잭션 밖 스레드). 본문이 바뀌면
  `lex_len`이 NULL로 돌아가 낡은 색인은 검색에서 빠진다. 기존 청크는 `knowledge-forge db backfill-bm25`.
- 질의: 질의를 같은 토큰으로 쪼개 Okapi BM25(k1=1.2, b=0.75)를 SQL로 합산한다. N·평균 길이는 색인된 청크 전체,
  df는 질의 term의 posting 수다(project/시간 조건은 후보만 거르고 IDF는 바꾸지 않는다).
- 적용 범위: `keyword_arm`의 본문 조회(자연어 `semantic_query`)만 바뀐다. 식별자·따옴표 구절(`lexical` 전략)과 제목
  조회(`document_search`)는 계속 pg_trgm - 식별자 전체 일치는 형태소 term으로 하면 `ERR_CONNECTION_RESET`이
  `err`/`connection`/`reset`으로 갈라져 오히려 약해진다. BM25가 0히트(미색인)면 trigram으로 되돌아가고,
  BM25가 돌면 term fallback(최대 10회 조회 + df 질의)은 건너뛴다. `steps`의 `lexical_search#semantic`에
  `ranker: bm25|trigram`이 남는다.
- 비용/한계: 색인은 청크당 고유 term 수만큼의 행이다. 질의 때 `count(*)`/`avg(lex_len)`를 매번 센다(청크 수만 개
  규모까지는 무시할 수준으로 보이나 측정하지 않았다 - 커지면 통계를 캐시). Kiwi 모델 로딩은 프로세스당 첫 호출에 약 0.5초.
