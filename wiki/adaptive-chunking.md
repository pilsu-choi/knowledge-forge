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

## 왜 슬라이딩 윈도우 오버랩이 없는가

일반적인 고정 토큰 청킹에서는 문맥 손실을 막기 위해 청크 간 오버랩을 두는 경우가 많다.
Knowledge Forge는 의도적으로 오버랩을 두지 않는다 — 구조 기반 청킹의 핵심은 애초에 청크
경계를 의미 단위(문서 구조) 경계에 맞추는 것이므로, 오버랩 없이도 문맥 손실이 최소화된다.
오버랩을 추가하면 오히려 동일 내용이 여러 Knowledge Object에 중복 추출되는 부작용이 생긴다.
이는 누락이 아니라 의도적인 설계 원칙이다.
