# Adaptive Chunker

Knowledge Forge는 고정 토큰 크기로 텍스트를 자르는 대신, **문서 구조 기반으로 청킹**한다.
핵심 원칙은 PRD 3장③이 명시한 그대로다.

> **가능한 가장 큰 Semantic Unit을 유지한다.**

## 계층 구조

```text
Document
 → Chapter    (Heading level 1 경계)
   → Section  (Heading level 2 경계)
     → Paragraph  (연속된 비-헤딩 요소 묶음)
       → Sentence  (문단이 예산 초과할 때만 생성)
         → Line    (문장까지 내려가도 예산 초과할 때만 생성)
```

상위 단위(예: Chapter 전체)가 설정된 토큰 예산(`max_tokens_per_chunk`, 기본값 4000,
`KF_MAX_TOKENS_PER_CHUNK` 환경변수 또는 `--max-tokens` CLI 옵션으로 조정) 이내라면 그 단위
그대로 하나의 `Chunk`가 된다. 예산을 넘을 때만 재귀적으로 한 단계 아래로 내려간다.

## 알고리즘

```python
def chunk_unit(unit, max_tokens, level):
    text = unit.render_text()
    if count_tokens(text) <= max_tokens:
        return [make_chunk(unit, level, text)]      # 가능한 가장 큰 semantic unit 유지
    next_level, children = unit.split_to_next_level()
    if children is None:                             # Line까지 내려가도 초과
        return hard_split_by_tokens(text, max_tokens)  # 최후 수단
    return [c for child in children for c in chunk_unit(child, max_tokens, next_level)]
```

## 예시로 보는 동작

토이 문서:

```text
# 1장. 서비스 개요            (Heading 1 → Chapter)
짧은 소개 문단 하나.          (Paragraph, 매우 짧음)

# 2장. 이용 약관               (Heading 1 → Chapter)
## 2.1 정의                   (Heading 2 → Section)
... (엄청나게 긴 정의 나열, 토큰 예산의 3배) ...
## 2.2 해지 조건               (Heading 2 → Section)
짧은 문단.
```

- **1장**: 전체가 예산 이내 → Chapter 레벨 청크 1개로 유지.
- **2장**: 전체는 예산 초과 → Section 단위로 분할 시도.
  - **2.1 정의**: Section 단위도 여전히 예산의 3배 → Paragraph 단위로 재귀 분할, 그래도
    한 문단이 너무 길면 Sentence 단위까지 내려가 여러 개의 Sentence 레벨 청크를 만든다.
  - **2.2 해지 조건**: Section 단위로도 이미 예산 이내 → Section 레벨 청크 1개로 유지.

즉 같은 문서 안에서도 청크의 `hierarchy_level`은 섞여 나올 수 있다 — 이것이 고정 토큰 청킹과의
근본적 차이다. 결과 청크 목록은 `parent_chunk_id`/`sequence_index`로 원래 순서와 계층 관계를
복원할 수 있게 기록된다.

## 트리 구성 규칙

- Heading level 1 → Chapter 경계.
- Heading level 1 밑, level 2 → Section 경계.
- Heading이 아닌 요소들의 연속 구간(`TextBlock`/`TableElement`/`ListElement`) → Paragraph
  하나로 그룹화.
- Sentence/Line은 미리 만들어두지 않고, 상위 Paragraph가 예산을 초과할 때만 그 자리에서
  생성한다(불필요한 세분화를 피하기 위함).

## 토큰 카운팅

`tiktoken`(`cl100k_base` 인코딩)을 로컬에서 빠르게 실행 가능한 휴리스틱으로 사용한다.

**한계**: `tiktoken`은 OpenAI 토크나이저이며 Claude의 실제 토크나이저가 아니다. 특히 한국어
텍스트에서는 오차가 더 커질 수 있다. 이를 완화하기 위해 실제 분할 임계값에 안전마진을 두어
`max_tokens`의 약 80%를 실질적인 분할 기준으로 사용한다. 이는 근사치이지 정확한 값이 아니라는
점을 명시적으로 인지하고 있어야 한다 — 필요하다면 Anthropic SDK의 `messages.count_tokens`로
최종 검증 단계를 추가하는 것을 향후 개선 과제로 남긴다.

## 문장 분리

문단이 예산을 초과해 Sentence 레벨까지 내려가야 할 때: 한국어 문장 분리 라이브러리 `kss`가
설치되어 있으면 우선 사용하고, 없으면 정규식 기반 폴백(`.`/`!`/`?`/`。`/`！`/`？` 뒤 공백 기준)을
사용한다. 정규식 폴백은 한국어의 종결어미(예: "~습니다", "~함") 패턴을 완벽히 인식하지 못할 수
있어 품질 리스크로 남아있다.

### 성능: kss에는 긴 조각만 넘긴다

`korean` extra의 `kss`는 mecab이 없는 환경이라 순수 파이썬 `pecab` 백엔드로 도는데, 비용이 입력
길이에 대략 2차로 는다(cProfile상 시간 대부분이 `pecab.Tokenizer.parse()`의 Viterbi 격자 탐색).
예전 `split_sentences()`는 토큰 예산을 넘는 문단 전체를 kss에 한 번에 넘겨, 줄바꿈 없는 PDF
문단이나 Slack 메시지 하나로 인제스트가 몇 분씩 멈췄다.

지금은 두 단계다.

1. 줄바꿈과 `_SENTENCE_BOUNDARY_RE`(문장부호 + 공백)로 먼저 싸게 자른다.
2. 그 조각 중 `_KSS_MIN_CHARS`(200자)를 넘는 것만 kss에 넘기고, 그것도 `_kss_windows()`로 250자
   이하 창으로 나눠 넣는다. 창은 마지막 공백 뒤에서 끊어 단어를 가르지 않고, 공백이 아예 없는
   구간만 글자 수로 끊는다(그런 구간은 어차피 `adaptive.py`의 `_hard_split()`이 토큰으로 다시 쪼갠다).

**대가:** 문장부호 없이 이어진 200자 이하 조각은 kss가 나눴을 문장("…조치 완료했습니다 추가
모니터링을…")을 하나로 둔다. 문장 분리는 문단이 예산을 넘을 때만 쓰이고 200자 조각은 현실적인
청크 예산보다 훨씬 작아서 청크 결과에는 영향이 없다고 봤다. 문장부호로 끝나는 샘플은 kss 단독
출력과 같았다.

**기각한 대안:** kss `backend="fast"`는 한/영 경계에서 잘못 병합하고, `"punct"`는 마침표 뒤에 공백이
없으면 못 나눈다. 1,000자 창만 두는 첫 수정안은 20,000자 문단에 41초가 걸렸다(창 하나의 비용도
초선형이라). mecab 같은 시스템 의존성은 범위 밖이라 넣지 않았다.

**측정** (개발 머신, pecab 백엔드. 문장부호 없는 첫 줄은 kss 로딩 시간 포함):

| 입력 | 수정 전 | 수정 후 |
|---|---|---|
| 반복 문장 3,280자 (문장부호 있음) | 39.1s | 0.0s |
| 비반복 문단 ~20,000자 (문장부호 있음) | 300초 넘게 끝나지 않음 | 0.0s |
| 비반복 문단 ~3,400자 (문장부호 없음) | - | 3.9s |
| 비반복 문단 ~17,700자 (문장부호 없음) | - | 7.2s |
| 공백 없는 "가" 5,000자 | - | 0.2s |
| `test_forces_sentence_and_line_fallback_...` | 105s | 0.4s |
| `test_every_emitted_chunk_respects_the_token_budget` | 30s | 3.3s |

남은 비용은 문장부호 없는 긴 문단의 약 0.4초/1,000자(선형)다. 더 줄이려면 mecab 백엔드가 필요하다.

## 들여쓰기 인식

각 `DocumentElement`(`ElementBase`)는 `indent_level: int | None` 필드를 가진다. 원본
문서에서 들여쓰기 신호를 읽어낼 수 있는 파서(DOCX/PDF/HWPX)가 채워 넣고, 신호가 없으면
`None`(= "0단계"가 아니라 "정보 없음")으로 둔다.

- **DOCX**: `paragraph_format.left_indent`(EMU)를 Word의 "들여쓰기 늘림" 버튼 한 단계인
  0.5인치(457200 EMU) 단위로 버킷화. 중첩 리스트 스타일(`List Bullet 2`, `List Number 3`
  등)도 리스트로 인식하도록 확장했고, 들여쓰기 단계가 바뀌면 새 `ListElement`로 분리해
  하위 항목이 상위 리스트의 `items`에 뭉개져 들어가지 않게 한다.
- **PDF**: 구조적 들여쓰기 마크업이 없으므로 줄의 `x0` 좌표를 페이지 최좌측 `x0` 대비
  18pt 단위로 버킷화하는 레이아웃 휴리스틱(제목 폰트 크기 비율, 단락 간격 비율과 같은 성격).
- **HWPX**: 문단 속성(`paragraph_property.margin.left`, HWPUNIT)을 850 HWPUNIT(~3mm)
  단위로 버킷화. 일부 HWPX 문서는 margin이 `hp:switch`/`hp:case`로 감싸져 있어
  `python-hwpx`가 이를 노출하지 않는 경우가 있는데, 이때는 예외를 던지지 않고 그냥
  `indent_level = None`으로 저하(degrade)한다.

`chunking.hierarchy.render_element_text()`가 `Heading`/`TextBlock`/`ListElement`
렌더링 시 `indent_level`만큼 앞에 공백(2칸/단계)을 붙인다 — Table/Image는 값이 있어도
무시한다(표 렌더링의 `" | "` 정렬이 깨지거나, VLM이 생성한 자유 텍스트에 접두어를 붙이는
것이 의미가 없기 때문). 청크 경계 자체를 들여쓰기로 결정하지는 않는다 — 각 `DocumentElement`는
이미 자신만의 Paragraph 노드이므로, 들여쓰기는 청크가 어디서 잘리는지가 아니라 청크 안
텍스트가 원본의 계층 구조를 얼마나 보존하는지에 반영된다.

## 표 행 단위 분리 (Row-aware split)

`TableElement`를 렌더링한 Paragraph가 예산을 초과하면, 문장/줄 분리 대신
`split_table_by_rows()`가 데이터 행 단위로 그리디하게 묶어 나눈다 — 일반 분리는 행 중간에서
잘라 어느 셀이 어느 컬럼인지 잃어버리므로 표에는 의미가 없다. 나뉜 청크마다 헤더 행(들)과
`caption`을 반복해 각 청크가 다른 청크 없이도 스스로 해석 가능하게 만든다.

`TableElement.header_rows`(기본 1)가 몇 개의 선행 행이 헤더인지 결정한다 — XLSX처럼 병합된
상위 헤더(예: "2025년"이 "기본급"/"가산율" 위에 걸쳐 있는 2단 헤더)가 있는 표는 `header_rows=2`
로 와서, 분리된 모든 청크에 두 헤더 행이 그대로 반복된다(파서가 헤더 행들을 하나로 합치는 대신
원본 그대로 유지 — 자세한 근거는 [`parsers.md`](parsers.md#xlsx-영역-분리-병합-셀-전파-다단-헤더)
참고). 단일 헤더 행만 있는 나머지 포맷(DOCX/PPTX/HWPX/HTML/Markdown 표)은 `header_rows=1`
기본값 그대로라 동작이 바뀌지 않는다.

## 왜 슬라이딩 윈도우 오버랩이 없는가

일반적인 고정 토큰 청킹에서는 문맥 손실을 막기 위해 청크 간 오버랩을 두는 경우가 많다.
Knowledge Forge는 의도적으로 오버랩을 두지 않는다 — 구조 기반 청킹의 핵심은 애초에 청크
경계를 의미 단위(문서 구조) 경계에 맞추는 것이므로, 오버랩 없이도 문맥 손실이 최소화된다.
오버랩을 추가하면 오히려 동일 내용이 여러 Knowledge Object에 중복 추출되는 부작용이 생긴다.
이는 누락이 아니라 의도적인 설계 원칙이다.

## `fix/etl-quick`: 표 행 분리가 O(행²)이던 문제

`split_table_by_rows()`는 원래 행을 하나씩 누적하면서, 새 행을 넣어볼 때마다 **그 시점까지
누적된 그룹 전체를 다시 `"\n".join(...)`으로 렌더링하고 `count_tokens()`로 다시
토큰화**했다. 행이 N개면 총 작업량이 O(N²)라 표가 커질수록 급격히 느려진다 — 실측으로
포항공장 생산 CAPA CHART(2021) xlsx(수천 행)가 이 함수 하나에서 7분+ 멈췄다
(`wiki/reingest-replay.md`의 "C단계" 참고, 이번에 그 원인을 고쳤다).

고친 방식: 고정 헤더 블록(캡션 + 헤더 행들)과 데이터 행 각각을 **최대 한두 번만** 토큰화해
두고, 그룹에 행을 추가할 때는 이미 알고 있는 값들을 더하고 빼는 것만으로 그룹 전체 토큰
수를 O(1)에 갱신한다. 단순히 "고정 토큰 + 각 행 토큰 + 줄바꿈 1토큰"을 더하기만 하면 될
것 같지만 실제로는 그렇지 않다 - BPE(`tiktoken` cl100k_base)가 어떤 조합에서는 한 줄의
마지막 글자와 그 뒤 개행 문자를 **하나의 토큰으로 합친다**(예: `"0%"` + `"\n"` →
`"0%\n"` 토큰 하나, 따로 셀 때보다 1개 적음). 이 융합은 항상 "그 줄 자신"과 "그 줄 바로
뒤 개행" 사이에서만 일어나므로, 각 줄을 **혼자일 때**(그룹의 마지막 줄 - `render()`가 뒤에
개행을 안 붙이는 경우)와 **뒤에 개행을 붙였을 때**(마지막이 아닌 경우) 두 가지로 미리
토큰화해두면, 그룹이 늘어날 때 "이전 마지막 줄"의 기여분을 (혼자 있을 때 값) →
(개행 붙인 값)으로 O(1)에 바꿔치기해서 정확히 재현할 수 있다.

`chunking/adaptive.py`의 사소한 중복도 같이 고쳤다: `_chunk_node()`가 리프 노드에서 이미
`count_tokens(node.text)`로 예산 이내인지 확인해놓고, `chunk()`가 그 결과를 저장할 때
같은 텍스트를 `count_tokens(item.text)`로 또 계산하고 있었다. `RenderedChunk`에
`token_count` 필드를 추가해 계산해둔 값을 그대로 넘긴다(모르는 경우만, 즉 hard-split
경로만 `chunk()`에서 새로 계산).

**검증**: `tests/chunking/test_hierarchy_table_split.py`가 옛 O(N²) 구현을 그대로 남겨
테스트 안의 오라클로 쓰고, 균일한 표/2단 헤더 표/셀 길이가 들쭉날쭉한 표 각각에서 새 구현이
옛 구현과 **정확히 같은 그룹**을 내는지 비교한다(여러 budget 값으로). 5,000행 표 성능
테스트는 3초 이내에 끝나는지 확인한다(개발 환경 실측 1초 미만). 무작위 유니코드 잡음으로
퍼징해보면 아주 드물게(200건 중 3건, 전부 인위적으로 짧은 budget과 무의미한 기호 조합)
그룹 경계가 한 행 어긋나는 경우가 있었지만, 어느 쪽도 예산 초과 그룹 수는 옛 구현과
**완전히 동일**했다 - 즉 "정상적인" 표 내용에서는 그룹이 그대로고, 최악의 경우에도 안전성
불변식(모든 행이 정확히 한 번씩만 나타난다, 단일 행이 이미 예산을 넘는 경우를 빼면 예산을
지킨다)은 깨지지 않는다.

## `fix/etl-quick`: NFC 정규화 시점을 청커 출력으로 앞당김

지금까지 NFC 정규화(`text_normalize.py::to_nfc`)는 `PostgresStore.save_chunks`가 DB 행에
쓸 때만 걸려 있었다 - `chunks.text` 컬럼은 NFC인데, `AdaptiveChunker.chunk()`가 돌려주는
메모리 상의 `Chunk.text`(파이프라인이 Knowledge Extraction 프롬프트와
`record_chunk_embeddings`에 바로 넘기는 그 값)는 파서가 만든 원본 그대로(macOS/Google
Drive 유래라면 NFD일 수 있음)였다. 즉 DB에 저장된 텍스트와 LLM/임베딩이 실제로 본 텍스트가
서로 다른 정규화 형태일 수 있었다.

이제 `AdaptiveChunker.chunk()`가 `Chunk`를 만드는 바로 그 지점에서 `to_nfc()`를 적용한다 -
`token_count`는 정규화 전 텍스트로 계산한 채 그대로 둔다(NFC 합성은 코드포인트 수만 줄이고,
애초에 청킹 시점의 근사치라 재계산할 가치가 없다는 기존 방침 유지). `save_chunks`는 이제
받은 `chunk.text`를 그대로 쓴다 - 두 곳에서 같은 정규화를 중복으로 걸 이유가 없다.

`documents.elements`(파서 원본)와 `documents.title`(별도 경로인
`storage/document_title.py::_clean`이 이미 `to_nfc`를 건다)은 이번 변경과 무관하다 - 전자는
의도적으로 원본을 보존하고, 후자는 이미 적재 시점에 정규화되고 있었다.
