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
| `confidence` | `float \| None` | 추출기가 매긴 0~1 신뢰도(item만, 범위 밖 값은 버림). entity와 summary fallback은 `None` |
| `version` | `int` | 기본 1. 같은 대상을 재추출해서 새 객체를 만들 때 증가 |
| `pipeline_version` | `str` | 이 객체를 만든 파이프라인 버전(`knowledge_forge.__version__`) |
| `source_hash` | `str` | 추출 시점 `Source.content_hash` — 원본이 바뀌었는지 나중에 비교할 수 있게 |
| `supersedes` | `str \| None` | 이 객체가 대체하는 이전 `knowledge_object_id`(재처리 이력, 옵션) |

### 타입별 `content` 모양

decision/action_item/event/entity는 타입별 Pydantic 모델(`models/knowledge_object.py`의
`CONTENT_MODELS`)이 모양을 정하고, `PostgresStore.save_knowledge_objects`가 저장 직전에
`validate_content`로 검증한다(빠진 선택 필드는 기본값으로 채워 저장, 모르는 키·틀린 값은
`ValidationError`). 나머지 타입은 여전히 `{"statement": ...}`이고 검증하지 않는다.

| `type` | 모델 | 필드 |
|---|---|---|
| `entity` | `EntityContent` | `name`, `entity_type`(LLM 라벨 그대로, 예: `PERSON`), `description` |
| `decision` | `DecisionContent` | `statement`, `decided_by: list[str]`, `decided_at: str \| None`, `status: proposed \| decided \| rejected \| None` |
| `action_item` | `ActionItemContent` | `statement`, `assignees: list[str]`, `due: str \| None`, `status: open \| in_progress \| done \| cancelled \| None` |
| `event` | `EventContent` | `statement`, `occurred_at: str \| None`, `participants: list[str]` |
| 그 외(`fact`/`requirement`/`policy`/`issue`/`concept`) | - | `{"statement": "지급 기한은 세금계산서 발행일로부터 30일 이내이다."}` |

- 세 item 모델은 `StatementContent`를 상속해 `statement`를 항상 가진다. `content["statement"]`만
  아는 소비자(OKF writer, 임베딩 텍스트)가 그대로 동작한다.
- 날짜는 문자열이다. 원문에 절대 날짜가 있으면 `YYYY-MM-DD`, 아니면 원문 표현("다음 주 월요일")
  그대로. 상대 날짜를 풀려면 문서 자체의 날짜가 필요한데 추출 단계에는 그게 확실히 없다.
- `content`가 여전히 `dict[str, Any]`인 이유: 새 타입을 추가할 때 `KnowledgeObject` 스키마와
  DB 마이그레이션을 건드리지 않게 하려는 것이다. 검증은 모델 생성 시점이 아니라 저장 시점에
  한다 — 그래야 자유 dict를 쓰는 기존 테스트·읽기 경로가 깨지지 않는다.
- 예전의 `Entity` 값 객체(`name`/`type`/`description`, 아무도 안 씀)는 `EntityContent`로
  흡수됐다. 필드 이름도 저장 모양에 맞춰 `type` → `entity_type`이다.

### `SourceRef` / `DocumentRef` / `Evidence` (Provenance 참조)

전체 `Source`/`Document`/`Chunk` 레코드를 임베드하지 않고 경량 참조만 담는 이유는
[`architecture.md`](architecture.md)의 "설계 결정" 절 참고.

| 타입 | 필드 |
|---|---|
| `SourceRef` | `source_id`, `source_type`, `uri` |
| `DocumentRef` | `document_id`, `title` |
| `Evidence` | `chunk_id`, `document_id`, `hierarchy_level`, `sequence_index` |

### `Entity` / `EntityMention` (`models/entity.py`)

ENTITY 타입 `KnowledgeObject`는 추출 한 번의 *언급*이다. `Entity`는 같은 프로젝트 안의 같은
이름 언급들이 해소되는 노드, `EntityMention`은 엔티티 ↔ KO 조인이다(DB 테이블은
[`storage.md`](storage.md)).

| `Entity` 필드 | 설명 |
|---|---|
| `entity_id` | `stable_id("entity", project_id, entity_type, name)` |
| `project_id` | 해소는 프로젝트를 절대 넘지 않는다 |
| `entity_type` | 정규화 타입(`person`/`organization`/`product`/`location`/`project`, 그 외는 소문자화한 원래 라벨) |
| `name` | 정규화 해소 키 |
| `display_name` | 처음 본 표기 |
| `aliases` | 이 엔티티로 해소된 표기 전부 |

| `EntityMention` 필드 | 설명 |
|---|---|
| `entity_id`, `knowledge_object_id` | 조인 양쪽 |
| `role` | `entity`(ENTITY KO 자신), `mentions`(그 ENTITY KO를 `MENTIONS`로 가리키는 item), `decided_by`, `assignee`, `participant` |
| `surface_form` | 그 KO에서의 표기 |
| `evidence` | 근거 chunk_id 목록(KO의 evidence) |

### 엔티티 해소 (`entity_resolution.py`)

LLM 호출 없는 규칙 기반이고 **정밀도 우선**이다. 잘못 합치면 엔티티로 펼치는 모든 소비자가
오염되고, 못 합치면 재현율만 잃는다. `(project_id, 정규화 타입, 정규화 이름)`이 정확히 같을
때만 합친다.

- **타입**: 알려진 동의어만 접는다(`PERSON`/`인물`/`사람` → `person`, `ORG`/`회사`/`팀` →
  `organization` 등). 모르는 라벨은 소문자화해서 그대로 쓰므로 다른 타입으로 섞이지 않는다.
  `date`/`amount`는 값이지 사물이 아니라서 해소하지 않는다.
- **이름 공통**: NFC, 공백을 모두 제거하고 casefold한다.
- **인물**: 뒤쪽 호칭·직함(`님`, `씨`, `팀장`, `대표이사`, `PM` 등)과 괄호 부가어(`(PM)`), 영문
  `Mr./Dr.`를 뗀다. 띄어 쓴 직함은 항상 떼고, 붙여 쓴 직함은 앞이 2음절 이상 한글 이름으로
  남을 때만 뗀다("정민찬팀장" → 정민찬, "김이사"는 그대로).
- **조직**: `㈜`, `(주)`, `주식회사`, `(유)`, `유한회사`를 뗀다.
- **버림**: 빈 문자열, 한 음절 한글(성만 남은 "김 이사"), 직함만 있는 것("팀장님"), 역할어
  ("담당자", "참석자", "고객" 등). 누구인지 모르면 엔티티를 만들지 않는다.
- **인물 속성**: decision `decided_by`, action_item `assignees`, event `participants`의 값은
  `person`으로 해소한다. 한 값에 여러 명이 들어오면(`"정민찬, 김철수"`, `"A 및 B"`) 나눈다.
- 하지 않는 것: 한영 표기 대응("정민찬" vs "Minchan Jeong"), 성만 있는 언급, 동명이인 구분.
  필요해지면 이 id 위에 병합 단계를 얹는다(id는 바뀌지 않는다).

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
ExtractionResult.entities[i]        -> KnowledgeObject(type=ENTITY, content=EntityContent)  (엔티티당 1개)
ExtractionResult.items[i]           -> KnowledgeObject(type=<item.type>)  (item당 1개, confidence 포함)
items[i].people/date/status         -> 타입별 content 필드 (decision: decided_by/decided_at/status, ...)
ExtractionResult.relations[i]       -> Relation(type=RELATED_TO)  (양쪽 이름이 모두 추출된 엔티티일 때만)
items[i].related_entities           -> Relation(type=MENTIONS, item -> entity)
items[i].caused_by_index            -> Relation(type=CAUSED_BY, item -> items[caused_by_index])
items[i].relates_to_indices         -> Relation(type=RELATED_TO, item -> items[idx])
```

`people`/`date`/`status`는 LLM 스키마에서 타입마다 따로 두지 않고 item 하나에 평평하게 둔다.
strict `json_schema`에서 작은 오픈소스 모델이 가장 안정적으로 채우는 모양이 평평한 선택 필드라서다.
`outcome.py::_item_content`가 이를 타입별 모델로 옮기고, 다른 타입의 status 어휘(decision에
`done`)는 저장하지 않고 버린다. 파이프라인은 저장 후 `resolve_entities`로 이 객체들을 엔티티로
해소한다(위 "엔티티 해소").

OpenRouter 경로는 `ExtractionResult.model_json_schema()`를 `_to_strict_json_schema`로 바꿔 보낸다.
모든 object에 `additionalProperties: false`와 전체 `required`를 넣고, strict 모드가 거부하는
`title`·`default` 키를 지운다(기본값은 응답 검증 때 Pydantic이 채운다).

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
  "content": {
    "statement": "배포를 월요일로 연기하기로 했다.",
    "decided_by": [],
    "decided_at": null,
    "status": "decided"
  },
  "topics": ["배포", "QA"],
  "document_type": "slack conversation",
  "extracted_at": "2026-09-01T12:34:56Z",
  "model_used": "claude-opus-5",
  "confidence": 1.0,
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
  ↕ EntityMention (role, evidence)
Entity (entity_id, project 범위)
```

파이프라인 실행 중에는 `Source`/`Document`/`Chunk`의 전체 레코드가 각각 한 번씩만 생성되고,
`KnowledgeObject`는 이들의 id를 참조로만 들고 있다. 전체 provenance를 복원하려면
`KnowledgeObject.source.source_id → document.document_id → evidence[].chunk_id`를 따라가면
되고, 다른 `KnowledgeObject`와의 연결은 `Relation.subject_id`/`object_id`를 따라가면 된다.

`document_id`로 원본 `Document`를 찾으면(저장이 켜져 있다면 PostgreSQL의 `documents` 테이블,
[`storage.md`](storage.md) 참고), 그 `elements`에서 페이지 내 정확한 위치(`bbox`)와 표/이미지가
VLM으로 추출됐는지 여부(`extraction_method`)까지 추적할 수 있다 —
[`vision-extraction.md`](vision-extraction.md) 참고.
