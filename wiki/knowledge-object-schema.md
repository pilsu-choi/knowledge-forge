# Knowledge Object 스키마

`KnowledgeObject`는 파이프라인의 최종 산출물이며, PRD 4장이 정의한 표준화된 JSON 형태다.
Pydantic 모델 정의는 `src/knowledge_forge/models/knowledge_object.py`.

## PRD 해석 노트 (중요)

PRD 4장의 예시 JSON은 다음 필드만 보여준다.

```json
{ "source": {}, "document": {}, "chunk": {}, "summary": "...", "facts": [], "entities": [], "relations": [] }
```

하지만 PRD 3장④(Knowledge Extraction)는 Entity/Relation/Summary/Key Facts와 함께
**Topic/Keyword**, **Document Type**도 추출 대상으로 명시한다. 4장 JSON에는 이 두 필드가
빠져 있는데, 이는 예시가 완전한 스키마가 아니라 형태를 보여주기 위한 발췌였다고 해석했다.
따라서 `KnowledgeObject`에는 `topics: list[str]`, `document_type: str`을 최상위 필드로
추가했다. 이 해석이 잘못되었다면(예: Document Type을 `document.metadata` 쪽에 두는 것이
맞다면) 스키마를 조정할 수 있다 — 지금은 설계 결정으로 명시적으로 남겨둔다.

## 필드 스펙

### `KnowledgeObject`

| 필드 | 타입 | 설명 |
|---|---|---|
| `knowledge_object_id` | `str` | 이 Knowledge Object의 고유 id |
| `source` | `SourceRef` | 원본 데이터 소스에 대한 경량 참조 |
| `document` | `DocumentRef` | 원본 Document에 대한 경량 참조 |
| `chunk` | `ChunkRef` | 이 Knowledge Object를 만든 Chunk에 대한 경량 참조 |
| `summary` | `str` | Chunk 내용 요약 |
| `facts` | `list[str]` | 추출된 핵심 사실(Key Facts) |
| `entities` | `list[Entity]` | 추출된 엔티티 |
| `relations` | `list[Relation]` | 추출된 관계 |
| `topics` | `list[str]` | 주제/키워드 (PRD 3장④, 4장 예시엔 없으나 위 해석에 따라 추가) |
| `document_type` | `str` | 문서 유형 (예: "계약서", "정책 문서", "회의록") |
| `extracted_at` | `datetime` | 추출 시각 |
| `model_used` | `str` | 추출에 사용된 LLM 모델 id |

### `SourceRef` / `DocumentRef` / `ChunkRef` (Provenance 참조)

전체 `Source`/`Document`/`Chunk` 레코드를 임베드하지 않고 경량 참조만 담는 이유는
[`architecture.md`](architecture.md)의 "설계 결정" 절 참고.

| 타입 | 필드 |
|---|---|
| `SourceRef` | `source_id`, `source_type`, `uri` |
| `DocumentRef` | `document_id`, `title` |
| `ChunkRef` | `chunk_id`, `hierarchy_level`, `sequence_index` |

### `Entity`

| 필드 | 타입 | 설명 |
|---|---|---|
| `name` | `str` | 엔티티 이름 |
| `type` | `str` | 엔티티 유형 (PERSON, ORG, PRODUCT, LOCATION, DATE 등 자유 문자열) |
| `description` | `str \| None` | 부가 설명 |

### `Relation`

| 필드 | 타입 | 설명 |
|---|---|---|
| `subject` | `str` | 주체 |
| `predicate` | `str` | 관계(서술어) |
| `object` | `str` | 대상 |
| `confidence` | `float \| None` | 신뢰도(옵션) |

## 예시 JSON

계약서 문서의 "지급 조건" 섹션 청크에서 추출된 Knowledge Object 예시:

```json
{
  "knowledge_object_id": "ko_8f2a1c...",
  "source": {
    "source_id": "src_1a2b3c...",
    "source_type": "local_file",
    "uri": "file:///data/contracts/vendor-agreement-2026.pdf"
  },
  "document": {
    "document_id": "doc_4d5e6f...",
    "title": "벤더 서비스 계약서"
  },
  "chunk": {
    "chunk_id": "chk_7g8h9i...",
    "hierarchy_level": "section",
    "sequence_index": 4
  },
  "summary": "본 섹션은 월 정산 방식과 지급 기한(세금계산서 발행일로부터 30일 이내)을 규정한다.",
  "facts": [
    "정산 주기는 매월 말일 기준이다.",
    "지급 기한은 세금계산서 발행일로부터 30일 이내이다.",
    "지연 지급 시 연 5%의 지연이자가 부과된다."
  ],
  "entities": [
    { "name": "㈜에이지소다", "type": "ORG", "description": "계약 당사자(공급사)" },
    { "name": "30일", "type": "DURATION", "description": "지급 기한" }
  ],
  "relations": [
    { "subject": "㈜에이지소다", "predicate": "지급받는다", "object": "월 정산 금액", "confidence": 0.92 }
  ],
  "topics": ["지급 조건", "정산 주기", "지연이자"],
  "document_type": "계약서",
  "extracted_at": "2026-09-01T12:34:56Z",
  "model_used": "claude-opus-5"
}
```

## Provenance 체인

```text
Source (source_id)
  ↓
Document (document_id)
  ↓
Chunk (chunk_id)
  ↓
KnowledgeObject (knowledge_object_id)
```

파이프라인 실행 중에는 `Source`/`Document`/`Chunk`의 전체 레코드가 각각 한 번씩만 생성되고,
`KnowledgeObject`는 이들의 id를 참조로만 들고 있다. 따라서 전체 provenance를 복원하려면
`KnowledgeObject.source.source_id → document.document_id → chunk.chunk_id`를 따라가면 된다.

`document_id`로 원본 `Document`를 찾으면(저장이 켜져 있다면 PostgreSQL의 `documents` 테이블,
[`storage.md`](storage.md) 참고), 그 `elements`에서 페이지 내 정확한 위치(`bbox`)와 표/이미지가
VLM으로 추출됐는지 여부(`extraction_method`)까지 추적할 수 있다 —
[`vision-extraction.md`](vision-extraction.md) 참고.
