# 검색·평가 로직 품질 조사 (2026-09-15)

main `60df97c` 기준으로 검색(`search/*`)과 평가(`eval/*`)의 품질을 조사한 결과다.
같은 날 수정과 A/B 결과는 맨 아래 "수정 내역"에 있다. [`search-code-review-2026-09-14.md`](search-code-review-2026-09-14.md)의 후속이다.
그 리뷰 이후 파싱 버그 수정·랭킹 A/B 채택·평가 재구현이 들어갔다.

✔ 표시는 직접 재현했거나 코드로 확인한 항목이다. 나머지는 병렬 리뷰 결과이며 따로 검증하지 않았다.
질의 해석 재현은 DB 없이 `extract_constraints_rules(q, now=2026-09-14T03:00Z)`로 했다(아래 "재현").

## 결론

- **검색**: 개별 방어 로직은 촘촘하다. 하지만 품질을 깎는 원인이 두 층에 걸쳐 있다.
  - **질의 해석**: 규칙이 필터를 너무 쉽게 켠다. retrieve는 작성자·참여자·확장자·기간 필터를 믿고 결과를 좁히거나 대체한다(`retrieve.py:410-417`). 그래서 오탐 하나가 곧 결과 손실이다.
  - **병합**: RRF가 문서 단위로 합산되지 않는다. 09-14 리뷰 결함 1이 아직 그대로다.
- **평가**: 틀은 깔끔하다. 인용문 라벨, 코퍼스 지문, dirty 트리 기록, paired bootstrap이 잘 되어 있다. 다만 두 가지에 주의가 필요하다.
  - 정답 라벨 확장이 현재 검색기에 기댄다.
  - none 정의가 튜닝 값에 묶여 있다.

## 1. 질의 해석 (`constraints.py`, `temporal.py`, `planner.py`, `router.py`)

### 높음

1. ✔ **일반 명사를 작성자·참여자로 뽑는다** (`constraints.py:405-415`)
   - 동사 목록에 맨 "작성"이 있고, 이름 앞 경계가 없다.

   | 질의 | 결과 |
   |---|---|
   | "보고서가 작성된 날짜" | `authors=['보고서']`, `semantic_query='된 날짜'` |
   | "김철수 팀장이 보낸 메일" | `authors=['팀장']` |
   | "reports from Google Drive" | `authors=['Google Drive']` |
   | "issues with Redis cluster" | `participants=['Redis']` |
   | "ASP.NET 마이그레이션", "Amazon.com 사례 분석" | 도메인 패턴이 `participants`로 흡수 |

2. ✔ **확장자·소스 마커가 어절 경계를 보지 않는다** (`constraints.py:711-722`, `:276-277`, `:533-539`)

   | 질의 | 결과 |
   |---|---|
   | "엑셀러레이터 프로그램" | `.xlsx` |
   | "API design doc" | `.docx` |
   | "유통 채널 전략" | `source_types=['slack']` |
   | "메일링 리스트" | `gmail`에 exhaustive까지 켜지고 `semantic_query='링 …'` |

   - 이런 필터가 있으면 시간 범위 완화도 막힌다(`retrieve.py:700-702`).
3. ✔ **멀티홉 플래너의 기간 해석이 따로 논다** (`planner.py:180`)
   - `parse_temporal_expression(augmented_query)` 하나만 쓴다.
   - **앞 단계 발췌 오염**: 발췌에 든 "어제"가 하위 질의의 기간 필터가 된다.
   - **`now` 미전달**: 기준 시각을 고정한 평가가 재현되지 않는다.
   - **연도 문맥 보정 누락**: `resolve_within_year`를 거치지 않아 "작년 3분기 매출 보고서"가 `2026년 Q3`가 된다. 09-14에 constraints 경로만 고쳤다.
   - 비교 대상별 기간 해석(`retrieve.py:1109`)도 같은 함수를 쓴다(미검증).
4. ✔ **식별자를 semantic_query에서 빼서 벡터 검색어의 핵심이 사라진다** (`constraints.py:659-670`)
   - "Wi-Fi 설정 방법" → `설정 방법`
   - "GPT-4 도입 효과" → `도입 효과`
   - "end-to-end 테스트" → 식별자로 잡혀 제목 조회가 켜진다.

### 중간

5. ✔ **비교 판정이 넓고 대상 분리가 깨진다** (`constraints.py:434`, `:739-746`, `:243`)
   - "효과 분석 결과 비교" → 대상이 `['효', '분석 결']`
   - "고객과 직원의 인식 변화" → 비교로 켜진다.
   - "작년 대비 올해 매출 변화" → 대상이 비고, 기간이 '작년'으로 남아 올해 문서가 걸러진다.
   - 비교 전략은 대상별 1위를 맨 앞에 고정한다(`retrieve.py:1141-1143`). 그래서 오판이 곧 상위 오염이다.
6. ✔ **새해 경계에서 연도가 틀린다** (`constraints.py:593-598`)
   - 연도를 KST가 아니라 UTC `now.year`로 잡는다.
   - now=2027-01-01 01:00 KST에서 "올해"가 2026년이 되고, "작년 3분기"가 2025 Q3가 된다.
   - 테스트가 이 경계를 피해 간다(`test_constraints.py:26-27`).
7. ✔ **"이월" 같은 한자어 달 이름 오탐** (`temporal.py:166-168`): "작년 예산 이월 내역"을 `2025년 2월`로 읽는다.
8. ✔ **따옴표·파일명 안의 날짜도 기간 필터가 된다** (`constraints.py:684`)
   - "'2024년 결산' 문서" → `2024년` 필터
   - "보고서_2025년_3월.pdf 요약" → `2025년` 필터
9. ✔ **의도 마커가 넓다** (`constraints.py:193`, `:228`)
   - "몇 개월 걸렸나" → `wants_count`에 `semantic_query='월 걸렸나'`
   - "권한이 없는 사용자 에러 해결 방법" → negative와 exhaustive(wide)로 켜진다.
10. ✔ **라우터가 사실상 무효다** (`router.py:93-102`, `planner.py:185-192`)
    - 두 소스가 있으면 모든 분기가 둘 다 돌려준다.
    - `router.py:39`의 `\b`가 한글 조사 앞에서 실패한다. `has_exact_match_signal("ERR_404에서 …")`가 False다.
    - `file_search`는 질의 전체를 한 덩어리 부분문자열로 비교한다(`service.py:95-99`). 발췌를 붙인 하위 질의로는 거의 0건이다.

### 낮음

11. ✔ **3음절 이상 명사의 끝 글자를 조사로 자른다** (`constraints.py:636`, `:949`)
    - "전문가 평가 정확도 개선 주간회의" → `전문 평가 정확 개선 주간회`
    - 09-14에 고친 것은 마커 판정이고, 이것은 term·semantic_query 경로다.
12. ✔ **따옴표 정규식이 영어 아포스트로피를 잡는다** (`constraints.py:357`): "What's the team's plan" → `quoted_phrases=['s the team']`
13. ✔ **keyword_terms에 어미·불용어가 남는다** (`constraints.py:918-958`)
    - `뭐였는지`, `알려줄`, `with`, `걸렸나`
    - 09-14 A/B에서 coverage SQL이 손실을 낸 원인과 같다.
14. **유지보수**
    - 경계 처리가 세 갈래다: 라틴 경계 변환, `_korean_eojeol_search`, 그냥 `re.search`. 2번과 12번이 여기서 나온다.
    - 따옴표·ID 정규식이 router와 constraints에 중복돼 있다.
    - 시간 해석 진입점이 둘(`_resolve_time_range`, `parse_temporal_expression`)이다.
    - 테스트 53개는 과거 회귀 위주다. 위 오탐 유형, 새해 경계, 플래너 발췌 오염은 테스트가 없다.

## 2. 검색 실행 (`retrieve.py`, `rerank.py`)

### 높음

1. ✔ **RRF가 문서 단위로 합산되지 않는다** (`rerank.py:40-51`) — 09-14 결함 1, 아직 그대로
   - 문서 검색·필터 히트에는 KO id, chunk id, path가 없어서 키가 `("id", source, id(hit))`가 된다. 그래서 다른 소스와 절대 합쳐지지 않는다.
   - 기본 `embedding_source`가 `knowledge_object`다(`config.py:236`). 벡터는 KO id, 어휘는 chunk id라 두 arm 키도 겹치지 않는다.
   - 결과적으로 여러 arm이 같은 문서를 찾아도 합의 가점이 없고, 소스별 순위를 번갈아 끼우는 것과 같다.

### 중상

2. ✔ **anchor 격차 비교의 부동소수점 버그** (`retrieve.py:763`, 상수 `:122-123`, `title_match.py:94`)
   - `1.0 - 0.8 >= 0.2`가 False다.
   - `title_match.py:37-38` 주석은 "0.8 상한이면 격차 0.2가 항상 유지된다"고 적었지만 실제로는 반대다.
   - 2위가 토큰 tier 0.8이면, 제목이 정확히 일치한 1위가 anchor를 잃고 `ambiguous`가 된다.
   - 수정: epsilon을 두거나, 0.8을 0.79로 내린다.

### 중간

3. **한 소스 리스트에 서로 다른 질의 순위가 이어 붙는다** (`:782`, `:807`, `:1143`, `:1165-1171`)
   - document_lookup, lexical, multihop, comparison이 해당한다. `:80-84` 주석이 스스로 금지한 방식이다.
   - 뒤쪽 질의 결과가 부당하게 밀리고, 같은 청크가 한 소스 안에서 이중으로 가산된다.
4. **투표자 수가 불균형하다** (`:969`, `:980`, `:1063`)
   - 어휘 쪽은 제목·본문에 term 소스가 최대 10개까지 붙고, 벡터는 1개뿐이다.
   - term fallback이 도는 질의에서는 어휘가 구조적으로 압도한다.
5. **병합 전 후보 풀이 작고, 벡터 하한이 절대값이다** (`:628`, `config.py:185`)
   - 소스별 limit×3 = 30개인데 단위가 청크/KO라 문서 수는 더 적다.
   - `vector_min_score` 0.45는 코사인 절대값이라, 임베딩 모델을 바꾸면 recall이 말없이 변한다.
6. ✔ **whole_document가 grain 채점 상위를 문서 순서로 채운다** (`:1436-1470`)
   - anchor 또는 상위 문서의 청크를 점수 1.0, 문서 내 순서로 맨 앞에 붙인다.
   - 그래서 chunk grain nDCG가 관련도가 아니라 문서 앞부분 순서로 결정된다.
7. **count 과대추정 가능(불확실)** (`:1550`): SQL COUNT 분기가 아니면 wide 후보 풀 크기(fuzzy·벡터 각 최대 200)가 개수 답이 된다.
8. **영문 섞인 토큰의 조사가 안 떨어진다** (`constraints.py:636`, `:949`)
   - "README를", "Drive에서"가 그대로 남아 ILIKE가 실패한다.
9. **grain 펼치기의 쿼리 폭주** (`:1507-1508`, `:1629`): grain 히트 없는 문서마다 `vector_search`를 1회씩 부르고, wide 질의는 절단하지 않는다.
10. **2글자 term 성능** (`:531-568`): 3글자 미만 패턴은 trigram 인덱스를 못 탄다. 09-14 S3의 남은 느린 꼬리와 같다.

### 낮음

- anchor 좁히기가 이미 추가된 벡터 히트에는 적용되지 않는다(`:885`, `:967-980`).
- 벡터 실패가 `RetrievalResult`에 드러나지 않는다(`:880-884`, `:1512-1517`). 평가가 어휘 단독 점수를 하이브리드로 착각할 수 있다.
- latest SQL의 metadata `::float`/`::boolean` 캐스팅에 예외 처리가 없다(`:1389-1394`, 불확실).
- 09-14 낮음 항목이 그대로 남아 있다.
  - `scores`의 척도 혼합(`:291-304`)
  - 도달 불가한 `filter_only`(`:1287`)
  - link_cohesion docstring 불일치
- `keyword_arm`의 step 라벨이 `document_lookup`/`lexical`이라 step 분석이 헷갈린다(`:972`, `:981`).
- 파일 절반가량이 run_id가 박힌 실측 서사 주석이다.

## 3. 평가 (`eval/`)

모두 코드를 읽고 확인했다.

1. ✔ **라벨 확장이 현재 검색기에 치우친다** (`generate.py` `expand_labels`)
   - 추가 정답은 현재 `retrieve` 상위 8개(`EXPANSION_TOP`)에서만 뽑는다.
   - 다른 방식이 다른 정답 청크를 찾으면 오답 처리된다. 풀링 편향이라 A/B가 현 방식에 유리하다.
   - 수정: 어휘 단독·벡터 단독 상위를 합친 풀에서 판정한다.
2. ✔ **none 정의가 튜닝 값에 묶여 있다**
   - 생성 게이트는 `vector_min_score`를 쓰고(`generate.py` `none`), 채점은 "채점 리스트가 비었나"를 본다(`run.py` `score_item`).
   - 하한만 올려도 none_correct가 오르니, 검색 품질이 아니라 임계값을 잰다.
   - 함께 볼 지표로 lookup 손실을 같이 보고하는 방법이 있다.
3. ✔ **overall이 서로 다른 지표의 평균이다** (`run.py` `summarize`)
   - lookup ndcg@10, exhaustive recall, none_correct를 섞는다.
   - 유형 비율(210/26/45)이 바뀌면 점수도 변하므로, 비교는 kind별로 볼 것.
4. ✔ **dev/test가 번호 짝홀로 나뉜다** (`generate.py` `generate_golden`)
   - 문서당 lookup이 최대 2개라, 같은 문서가 dev와 test 양쪽에 들어갈 수 있다.
   - dev로 튜닝하면 test가 오염된다. 문서 id 해시로 나눌 것.
5. ✔ **grain id 없는 히트는 점수 0이다** (`goldenset.py` `ranked_ids`)
   - 제목·필터로 맞힌 문서가 채점에서 빠진다.
   - v1 기준점에서 lookup 빈 리스트 31/30건, exhaustive recall 0.042/0.013의 원인이다.
   - exhaustive에는 컷오프가 없고(`EXHAUSTIVE_LIMIT` 200), 원인은 top-N 절단이 아니라 이것이다.
6. ✔ **exhaustive 질의가 필터 해석 테스트다**
   - 예: "구글 드라이브에서 pptx 파일을 전수 조회해 주세요."
   - 검색 품질보다 `constraints` 규칙 적중을 잰다. 술어 후보도 26/45뿐이다.
7. ✔ **exhaustive stale 판정이 엄격하다** (`goldenset.py` `resolve_gold`): 술어 결과가 문서 하나만 달라져도 항목 전체가 stale이다. 증분 수집이 돌면 금방 줄어든다.
8. **e2e judge 보정이 없다**: gpt-4o-mini 단일 judge이고, 사람 라벨과의 일치율을 잰 적이 없다.
9. **골든셋 전 항목이 `status: draft`다**: 스팟체크 24개가 아직 대기 중이다.

## 추천 순서

1. **질의 해석 필터 강등**: 오탐이 잦은 추출(작성 동사, 영어 from/with, 도메인, 확장자·소스 마커)을 믿는 필터에서 힌트로 내린다. 마커에는 어절 경계를 건다.
2. **작은 버그**
   - anchor 격차 epsilon
   - 식별자를 semantic_query에 남기기
   - 플래너에 `_resolve_time_range`·`now` 전달(발췌는 시간 파싱에서 제외)
   - 새해 KST 연도
3. **문서 단위 RRF**: 문서 히트에도 document 키를 준다. 효과가 클 수 있지만 A/B로 확인한다.
4. **평가 보정**
   - kind별 보고
   - 문서 단위 dev/test 분할
   - 다중 arm 라벨 풀
   - 오탐 유형을 담은 질의 해석 단위 테스트

## 재현

질의 해석 probe는 DB가 필요 없다. 핵심 호출:

```python
from datetime import datetime, timezone
from knowledge_forge.search.constraints import extract_constraints_rules, keyword_terms
from knowledge_forge.search.temporal import parse_temporal_expression
from knowledge_forge.search.router import has_exact_match_signal

NOW = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc)
extract_constraints_rules("보고서가 작성된 날짜", now=NOW)          # authors=['보고서']
extract_constraints_rules("엑셀러레이터 프로그램 지원 현황", now=NOW)  # file_extensions=['.xlsx', '.xls']
extract_constraints_rules("올해 계획서", now=datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc))  # 2026년
parse_temporal_expression("작년 3분기 매출 보고서", now=NOW)        # 2026년 Q3
has_exact_match_signal("ERR_404에서 뭐가 문제야")                  # False
```

소수점 오차 확인: `python3 -c "print(1.0 - 0.8 >= 0.2)"` → `False`.

## 수정 내역 (integrate/search-quality-2026-09-15)

2026-09-15, 병렬 작업 4갈래를 통합 브랜치로 모았다(base main `60df97c`).

| 커밋 | 내용 | 결정 |
|---|---|---|
| `8fbf17e` | 질의 제약 규칙 정밀도(1장 #1·2·4·5·6·8·9·11·12·13·14) | 채택 |
| `50474a1` | 플래너 시간 파싱·"이월"·라우터(1장 #3·7·10) | 채택 |
| `fe29cde` | 평가 라벨 풀링·none 임계값 기록·overall·dev/test 분할(3장 #1~4) | 채택 |
| `82f4548` | anchor 격차 epsilon, `RetrievalResult.vector_error`(2장 #2, 낮음) | 채택 |
| `77aa7a2` | 질의·term별 순위 리스트 분리, 한 리스트 안 중복은 한 표(2장 #3) | 채택(회귀 없음, 개선 근거도 없음) |
| `7dd8080`, `7942f80` | 문서 합의 가점(2장 #1)과 소스 계열 한 표 변형 | **기각** |
| `7ba8526` | whole_document 관련도 순(2장 #6) | **보류** - 골든셋 281개 중 이 경로가 켜진 항목 0, `fix/ranking-merge-on-integrate`에만 있음 |

### A/B (retrieval, 골든셋 v1, lookup ndcg@10 delta [95% CI] 승/패)

| 비교 | split | chunk | KO |
|---|---|---|---|
| 통합 vs main | dev | −0.003 [−0.027, +0.024] 5/7 | +0.015 [−0.011, +0.043] 10/6 |
| | test | **+0.037 [+0.003, +0.076]** 11/6 | **−0.014 [−0.029, −0.002]** 2/9 |
| 문서 합의 가점(ac5d9d5 대비) | test | −0.025 [−0.057, +0.008] 16/26 | +0.027 [−0.001, +0.056] 18/9 |

- 통합 vs main에서 점수가 바뀐 lookup 56건은 **전부 질의 해석 변화 때문**이다(구 main 규칙과 새 규칙의 파싱을 항목별로 대조, 파싱이 같은데 점수가 바뀐 항목 0).
  - 한 방향 결함은 없었다. keyword_terms에서 의문 어미를 뺀 것과 식별자를 semantic_query에 남긴 것이 둘 다 큰 이득(+1.0, +0.63)과 큰 손실(−0.57, −0.39)을 함께 냈다.
- KO는 dev와 test의 부호가 반대라 두 split을 합치면 0에 가깝고, chunk는 순이득이라 채택했다. 09-14 랭킹 A/B가 KO lookup 소폭 손실을 안고 채택한 것과 같은 판단이다.
- 문서 합의 가점이 크게 진 항목은 모두 term fallback이 돈 질의다. term 리스트가 최대 10개라 약한 문서가 리스트 수만큼 표를 받았다(2장 #4). 소스 계열당 한 표로 막아도 grain 사이 엇갈림이 남아 09-14의 858179c와 같은 결론이다.
- 실행 간 노이즈: 같은 코드를 다시 돌려도 KO `lookup-006`이 벡터 거리 동점 순서 때문에 0.613 ↔ 0.387로 흔들린다. 그 밖의 항목은 재실행 차이가 없었다.
- 결과 JSON: scratchpad `eval-runs-ranking/`(세션 임시 디렉터리). 코퍼스 지문은 모든 실행에서 동일(문서 358, 임베딩 chunk 2,856 / KO 5,107).

### 항목별 상태

- **1장(질의 해석)**
  - 수정: #1, #3~#10, #12.
  - 부분 수정:
    - #2: "채널"은 슬랙 힌트로 남김(골든셋의 "채널" 질의 대부분이 슬랙 채널).
    - #11: "위원회의"의 "의", "분석결과"처럼 붙여 쓴 낱말은 여전함.
    - #13: keyword_terms만 고침, semantic_query에는 의문 어미가 남음.
    - #14: router와의 정규식 중복은 router 쪽에서 constraints를 import하는 방식으로 해소.
- **2장(검색 실행)**
  - 수정: #2, #3, 벡터 실패 노출.
  - 기각: #1(A/B).
  - 보류: #6(측정 불가).
  - 미착수: #4·#5(합의 가점 기각 뒤 문제 항목이 드러나지 않음), #7·#9·#10, 나머지 낮음.
- **3장(평가)**
  - 수정: #1, #3.
  - 부분 수정:
    - #2: 게이트 임계값을 골든셋에 기록하고 compare가 설정 차이를 경고함. 채점 규칙 자체는 그대로.
    - #4: 새로 생성하는 골든셋부터 적용. v1 파일은 옛 짝홀 분할.
  - 미착수: #5~#9.

### 결과 형식 변경

- 실행 결과 `summary.overall`은 `{n, stale, error}`만 남는다. `score`는 kind별 요약에서 본다.
- 골든셋 JSON에 `corpus.none_gate.vector_min_score`, `items[].document_id`가 붙는다(신규 생성분만, 둘 다 옵셔널).
- `route_query`는 소스 순서가 고정이고 `reason` 문자열만 분기별로 다르다.
- `plan_and_search(..., now=)` 인자가 추가됐다.

### A/B 항목 대조에서 새로 보인 파싱 잔여 결함

- "누구인가" → `누구인`, "진행되는" → `진행되`: "가"·"는"을 조사로 잘못 뗀다.
- 문서 참조 식별자("2026-022_PRJ-2026-022 문서", "2026-038-c 프로젝트")는 여전히 semantic_query에서 빠진다(#4는 일반 식별자만 고쳤다).
- "/aegis-email" → `/ aegis-email`로 쪼개진다.
- "올해 5월 15일 회의록"이 올해 전체 기간으로 해석된다(`temporal.py::resolve_within_year`).

### 검증

- 테스트 DB는 개발 DB가 아니라 스크래치 DB `knowledge_forge_itest`를 썼다. 개발 DB에 테스트 행을 넣으면 진행 중인 A/B의 문서 지문이 바뀌기 때문이다.
- `tests/api`는 Redis(6379)가 없어 main에서도 같은 9건이 실패하므로 제외했다.
- 통합 HEAD 기준 `pytest tests --ignore=tests/api` 1041 passed(DB 포함), `ruff check src tests`·`mypy src` 통과.
- 머지 접점에서 테스트 2건을 고쳤다. 코드 동작은 그대로다.
  - 가짜 planner가 새 `now` 인자를 받도록 했다(`tests/eval/test_grain.py`).
  - `store_hits`가 소스 계열(`#` 앞)로 거르도록 했다(`tests/search/test_vector_path.py`). `lexical_search#<질의>` 이름 분리를 비개발 DB 테스트가 못 잡았다.
