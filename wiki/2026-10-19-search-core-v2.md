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
