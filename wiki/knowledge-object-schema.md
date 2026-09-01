# Knowledge Object 스키마

`KnowledgeObject`는 파이프라인의 최종 산출물이며, `Relation`은 여러 `KnowledgeObject`를
잇는 typed edge다. Pydantic 모델 정의는 `src/knowledge_forge/models/knowledge_object.py`,
`src/knowledge_forge/models/relation.py`.

## 설계 노트: 왜 PRD 4장 예시 JSON 그대로가 아닌가 (중요)

PRD 4장의 예시 JSON은 `{ source, document, chunk, summary, facts, entities, relations }`라는
단일 flat bag 형태를 보여준다. 초기 구현은 이 형태를 그대로 따랐다(청크 하나 → `KnowledgeObject`
하나, `chunk`는 단일 `ChunkRef`). 하지만 이 형태에는 두 가지 구조적 문제가 있었다:

1. **청크와 KnowledgeObject는 1:1이 아니다.** 청크 하나(예: Slack 대화 한 덩어리)가 서로
   독립적으로 참조 가능한 여러 knowledge(하나의 Decision, 그 Decision의 Reason, 관련
   ActionItem)를 담을 수 있고, 반대로 하나의 Decision이 여러 청크에 걸쳐 근거를 가질 수도
   있다. 단일 `KnowledgeObject`에 `facts: list[str]`처럼 flat list로 몰아넣으면 이 구조가
   사라진다.
2. **관계(relation)가 텍스트 triple로 KnowledgeObject 안에 갇혀 있으면 그래프가 안 된다.**
   `{subject: "Acme", predicate: "signed", object: "the agreement"}`는 자유 텍스트일 뿐,
   다른 `KnowledgeObject`를 id로 가리키지 않는다 — "이 Decision이 어떤 Issue 때문인지",
   "이 Fact가 어떤 Entity를 언급하는지"를 나중에 그래프로 순회할 수 없다.

그래서 스키마를 다음처럼 바꿨다:

- `KnowledgeObject`는 **typed envelope**이다 — `type`(`KnowledgeObjectType`)이 Entity/Fact/
  Event/Decision/ActionItem/Requirement/Policy/Issue/Concept 중 하나를 가리키고, 타입별
  payload는 `content: dict`에 담는다(고정된 facts/entities/relations 필드가 아니라).
- `chunk: ChunkRef` 단일 필드 대신 `evidence: list[Evidence]` — 이 `KnowledgeObject`를
  뒷받침하는 청크가 여러 개일 수 있다는 걸 데이터 모델이 표현한다.
- 관계는 `KnowledgeObject` 안에 내장되지 않고 별도의 `Relation` 레코드로, 두
  `knowledge_object_id`를 typed edge로 잇는다.
- 재처리(re-extraction)를 표현할 수 있게 `version`/`pipeline_version`/`source_hash`/
  `supersedes`를 추가했다.

Topic/Keyword(PRD 3장④)는 여전히 `topics: list[str]`로, Document Type도 여전히
`document_type: str`로 최상위에 둔다 — 이 부분은 기존 해석을 유지했다. 자세한 설계 배경은
[`architecture.md`](architecture.md)의 "설계 결정: Chunk와 KnowledgeObject는 1:1이 아니다"
절 참고.

## 필드 스펙

### `KnowledgeObject`

| 필드 | 타입 | 설명 |
|---|---|---|
| `knowledge_object_id` | `str` | 이 Knowledge Object의 고유 id |
| `type` | `KnowledgeObjectType` | `entity`/`fact`/`event`/`decision`/`action_item`/`requirement`/`policy`/`issue`/`concept` |
| `source` | `SourceRef` | 원본 데이터 소스에 대한 경량 참조 |
| `document` | `DocumentRef` | 원본 Document에 대한 경량 참조 |
| `evidence` | `list[Evidence]` | 이 객체를 뒷받침하는 청크 목록(각 `chunk_id`/`document_id`/`hierarchy_level`/`sequence_index`) — 여러 개일 수 있다 |
| `summary` | `str` | 이 객체 자체의 요약(청크 전체 요약이 아니라, 이 하나의 entity/item에 대한 요약) |
| `content` | `dict` | 타입별 payload — 아래 "타입별 `content` 모양" 참고 |
| `topics` | `list[str]` | 주제/키워드 (PRD 3장④) |
| `document_type` | `str` | 문서 유형 (예: "계약서", "정책 문서", "회의록") |
| `extracted_at` | `datetime` | 추출 시각 |
| `model_used` | `str` | 추출에 사용된 LLM 모델 id |
| `version` | `int` | 기본 1. 같은 대상을 재추출해서 새 객체를 만들 때 증가 |
| `pipeline_version` | `str` | 이 객체를 만든 파이프라인 버전(`knowledge_forge.__version__`) |
| `source_hash` | `str` | 추출 시점 `Source.content_hash` — 원본이 바뀌었는지 나중에 비교할 수 있게 |
| `supersedes` | `str \| None` | 이 객체가 대체하는 이전 `knowledge_object_id`(재처리 이력, 옵션) |

### 타입별 `content` 모양

| `type` | `content` 예시 |
|---|---|
| `entity` | `{"name": "㈜에이지소다", "entity_type": "ORG", "description": "계약 당사자"}` |
| 그 외 전부(`fact`/`event`/`decision`/`action_item`/`requirement`/`policy`/`issue`/`concept`) | `{"statement": "지급 기한은 세금계산서 발행일로부터 30일 이내이다."}` |

`content`가 `dict[str, Any]`(고정 스키마가 아님)인 이유: `KnowledgeObjectType`에 새 타입을
추가할 때마다 `KnowledgeObject` 자체의 Pydantic 스키마를 바꾸지 않아도 되게 하기 위해서다 —
[`architecture.md`](architecture.md) 참고.

### `SourceRef` / `DocumentRef` / `Evidence` (Provenance 참조)

전체 `Source`/`Document`/`Chunk` 레코드를 임베드하지 않고 경량 참조만 담는 이유는
[`architecture.md`](architecture.md)의 "설계 결정" 절 참고.

| 타입 | 필드 |
|---|---|
| `SourceRef` | `source_id`, `source_type`, `uri` |
| `DocumentRef` | `document_id`, `title` |
| `Evidence` | `chunk_id`, `document_id`, `hierarchy_level`, `sequence_index` |

### `Entity`

`content`가 `entity` 타입일 때의 payload 모양과 같은 필드를 가진, 재사용 가능한 값 객체
(추출 코드/테스트에서 편의상 사용 — `KnowledgeObject.content`에는 결국 `model_dump()`된
dict로 들어간다).

| 필드 | 타입 | 설명 |
|---|---|---|
| `name` | `str` | 엔티티 이름 |
| `type` | `str` | 엔티티 유형 (PERSON, ORG, PRODUCT, LOCATION, DATE 등 자유 문자열) |
| `description` | `str \| None` | 부가 설명 |

### `Relation`

| 필드 | 타입 | 설명 |
|---|---|---|
| `relation_id` | `str` | 이 Relation의 고유 id |
| `type` | `RelationType` | `mentions`/`related_to`/`caused_by`/`depends_on`/`references`/`supersedes` |
| `subject_id` | `str` | 시작 쪽 `knowledge_object_id` |
| `object_id` | `str` | 도착 쪽 `knowledge_object_id` |
| `label` | `str \| None` | 원본 predicate 텍스트(추출된 subject-predicate-object triple에서 왔다면, 가독성용) |
| `confidence` | `float \| None` | 신뢰도(옵션) |
| `evidence` | `list[str]` | 이 관계를 뒷받침하는 chunk_id 목록 |
| `created_at` | `datetime` | 생성 시각 |

## 추출 결과 → KnowledgeObject/Relation 매핑

LLM이 실제로 반환하는 건 `KnowledgeObject`/`Relation`이 아니라 `ExtractionResult`
(`src/knowledge_forge/extraction/schemas.py`) — entities/relations(엔티티 간 텍스트
triple)/items(타입드 knowledge item)/topics/document_type. `extraction/outcome.py::
build_extraction_outcome()`이 청크당 한 번씩 이를 다음처럼 펼친다:

```text
ExtractionResult.entities[i]        -> KnowledgeObject(type=ENTITY)  (엔티티당 1개)
ExtractionResult.items[i]           -> KnowledgeObject(type=<item.type>)  (item당 1개)
ExtractionResult.relations[i]       -> Relation(type=RELATED_TO)  (양쪽 이름이 모두 추출된 엔티티일 때만)
items[i].related_entities           -> Relation(type=MENTIONS, item -> entity)
items[i].caused_by_index            -> Relation(type=CAUSED_BY, item -> items[caused_by_index])
items[i].relates_to_indices         -> Relation(type=RELATED_TO, item -> items[idx])
```

엔티티도 item도 하나도 없는 청크(순수 연결어/설명문)는 `ExtractionResult.summary`를 담은
`FACT` 타입 `KnowledgeObject` 하나로 fallback한다 — 청크에 내용이 있는데 결과가 0개
`KnowledgeObject`가 되는 일은 없다.

## 예시 JSON

Slack 대화 "배포 언제 할까요? / QA 아직 안 끝났어요 / 그럼 월요일로 미루죠"에서 추출된
Issue와 그로 인한 Decision, 그리고 둘을 잇는 Relation 예시:

```json
{
  "knowledge_object_id": "ko_issue_1a2b...",
  "type": "issue",
  "source": { "source_id": "src_9f...", "source_type": "slack", "uri": "slack://general/episode/1700000000.0" },
  "document": { "document_id": "doc_4d...", "title": "Slack #general 대화" },
  "evidence": [
    { "chunk_id": "chk_7g...", "document_id": "doc_4d...", "hierarchy_level": "section", "sequence_index": 0 }
  ],
  "summary": "QA가 아직 끝나지 않았다.",
  "content": { "statement": "QA가 아직 끝나지 않았다." },
  "topics": ["배포", "QA"],
  "document_type": "slack conversation",
  "extracted_at": "2026-09-01T12:34:56Z",
  "model_used": "claude-opus-5",
  "version": 1,
  "pipeline_version": "0.1.0",
  "source_hash": "b7e2...",
  "supersedes": null
}
```

```json
{
  "knowledge_object_id": "ko_decision_3c4d...",
  "type": "decision",
  "source": { "source_id": "src_9f...", "source_type": "slack", "uri": "slack://general/episode/1700000000.0" },
  "document": { "document_id": "doc_4d...", "title": "Slack #general 대화" },
  "evidence": [
    { "chunk_id": "chk_7g...", "document_id": "doc_4d...", "hierarchy_level": "section", "sequence_index": 0 }
  ],
  "summary": "배포를 월요일로 연기하기로 했다.",
  "content": { "statement": "배포를 월요일로 연기하기로 했다." },
  "topics": ["배포", "QA"],
  "document_type": "slack conversation",
  "extracted_at": "2026-09-01T12:34:56Z",
  "model_used": "claude-opus-5",
  "version": 1,
  "pipeline_version": "0.1.0",
  "source_hash": "b7e2...",
  "supersedes": null
}
```

```json
{
  "relation_id": "rel_5e6f...",
  "type": "caused_by",
  "subject_id": "ko_decision_3c4d...",
  "object_id": "ko_issue_1a2b...",
  "label": null,
  "confidence": null,
  "evidence": ["chk_7g..."],
  "created_at": "2026-09-01T12:34:56Z"
}
```

## Provenance 체인

```text
Source (source_id)
  ↓
Document (document_id)
  ↓
Chunk (evidence[].chunk_id, 여러 개 가능)
  ↓
KnowledgeObject (knowledge_object_id)
  ↕ Relation (subject_id / object_id)
KnowledgeObject (knowledge_object_id)
```

파이프라인 실행 중에는 `Source`/`Document`/`Chunk`의 전체 레코드가 각각 한 번씩만 생성되고,
`KnowledgeObject`는 이들의 id를 참조로만 들고 있다. 전체 provenance를 복원하려면
`KnowledgeObject.source.source_id → document.document_id → evidence[].chunk_id`를 따라가면
되고, 다른 `KnowledgeObject`와의 연결은 `Relation.subject_id`/`object_id`를 따라가면 된다.

`document_id`로 원본 `Document`를 찾으면(저장이 켜져 있다면 PostgreSQL의 `documents` 테이블,
[`storage.md`](storage.md) 참고), 그 `elements`에서 페이지 내 정확한 위치(`bbox`)와 표/이미지가
VLM으로 추출됐는지 여부(`extraction_method`)까지 추적할 수 있다 —
[`vision-extraction.md`](vision-extraction.md) 참고.
