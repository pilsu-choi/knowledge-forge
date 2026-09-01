# OKF — 파일 기반 Knowledge Base

`knowledge-forge extract --okf-dir <dir>`을 켜면, `PostgresStore`(선택)와는 독립적으로
같은 파이프라인 산출물을 파일 트리로도 기록한다. Postgres 없이도(또는 Postgres와 나란히)
사람이 읽고 git으로 diff할 수 있는 Knowledge Base가 필요하다는 문제의식에서 나왔다 — 자세한
배경은 [`architecture.md`](architecture.md)의 "Hierarchy는 Knowledge Base의 유일한 조직
원리가 아니다" 절 참고.

## 왜 필요한가

`PostgresStore`는 질의에 강하지만(조인, JSONB 필터) 데이터베이스가 있어야만 접근할 수 있고,
레코드 하나를 눈으로 확인하려면 `psql`을 열어야 한다. OKF는 그 반대 극단을 채운다:
데이터베이스 없이도 존재하고, 파일 하나 = 레코드 하나라 grep/diff/git으로 다룰 수 있고,
Graph DB나 검색 인덱스를 나중에 붙일 때 그 인덱스를 만들어내는 "source of truth" 파일
포맷 역할을 할 수 있다(PRD가 제외한 Graph DB/Search 범위를 지금 막지 않는다는 원칙은
[`roadmap.md`](roadmap.md) 참고).

## 디렉터리 레이아웃

```text
<okf-dir>/
  manifest.yaml
  resources/{source_id}.yaml
  objects/{type}/{knowledge_object_id}.yaml
  relations/{relation_id}.yaml
  views/by_topic.yaml
  views/by_document.yaml
  views/timeline.yaml
```

- **`resources/`**: `Source` 하나당 파일 하나. `Source` 레코드 전체 + 해당 `Document`의
  가벼운 포인터(`document_id`/`title`/`element_count`)만 담는다 — `Document.elements` 전체
  트리는 담지 않는다(파이프라인 자체 저장소, 즉 `PostgresStore`가 켜져 있다면 그쪽이 정본).
- **`objects/{type}/`**: `KnowledgeObject.type`(`entity`/`fact`/`event`/`decision`/
  `action_item`/`requirement`/`policy`/`issue`/`concept`)별 하위 디렉터리에 `KnowledgeObject`
  전체를 하나씩 저장한다 — 타입별로 나눈 것 자체가 하나의 조직 관점(organization view)이다.
- **`relations/`**: `Relation` 전체를 하나씩 저장한다. `subject_id`/`object_id`가 다른
  `KnowledgeObject`의 파일명(확장자 제외)과 일치하므로, 그래프를 따라가려면 id로 파일을
  열면 된다.
- **`views/`**: 타입별 view 말고 다른 조회 관점 — `by_topic.yaml`(topic → 객체 목록),
  `by_document.yaml`(document_id → `{title, objects}`, Project의 대용 — 스키마에 별도 Project
  개념은 없다), `timeline.yaml`(`extracted_at` 기준 시간순 목록). 세 파일 모두 각 객체 레코드의
  `topics`/`document`/`extracted_at` 필드를 읽어 만든 **파생 인덱스**다 — `manifest.yaml`처럼
  레코드 자체는 건드리지 않고 `objects/`를 다시 스캔해서 매번 재생성된다.

## `manifest.yaml`은 카탈로그이지 Knowledge 저장소가 아니다

`manifest.yaml`은 각 레코드의 **id + type + 상대 경로**만 담는 인덱스다:

```yaml
generated_at: '2026-09-01T12:00:00+00:00'
pipeline_version: 0.1.0
resources:
  - {id: src_..., type: local_file, path: resources/src_....yaml}
objects:
  - {id: ko_..., type: decision, path: objects/decision/ko_....yaml}
relations:
  - {id: rel_..., type: caused_by, path: relations/rel_....yaml}
```

전체 knowledge 내용(요약, content, evidence 등)은 각 레코드 파일에만 있다 — manifest는
"무엇이 있고 어디에 있는지"만 답한다. 이렇게 나눈 이유:

1. Knowledge Base가 커질수록 manifest 하나에 모든 내용을 담으면 그 파일 자체가 병목이 된다.
2. `manifest.yaml`은 **트리를 스캔해서 매번 다시 생성**된다(누적된 in-memory 상태가 아니다) —
   그래서 여러 번의 파이프라인 실행(예: `knowledge-forge schedule`의 증분 동기화)이 같은
   `--okf-dir`에 쓰더라도, 마지막 실행 한 번의 매니페스트만 남는 대신 트리에 쌓인 모든
   레코드가 항상 정확히 반영된다.

## `OKFWriter` (`src/knowledge_forge/okf/writer.py`)

```python
writer = OKFWriter(root)
writer.write_resource(source, document)   # Source당 1회
writer.write_objects(outcome.objects)     # 문서당 (병합된 ExtractionOutcome 1개)
writer.write_relations(outcome.relations) # 문서당
writer.write_manifest()                   # 실행 끝에 1회 — 트리 스캔 후 재생성
writer.write_views()                      # 실행 끝에 1회 — 트리 스캔 후 재생성
```

`pipeline.run_pipeline(..., okf_writer=writer)`를 넘기면 위 `write_resource`/`write_objects`/
`write_relations` 호출을 파이프라인이 알아서 각 단계에서 수행한다 — `write_manifest()`/
`write_views()`만 호출자(CLI)가 파이프라인 실행이 끝난 뒤 한 번씩 호출하면 된다(`cli.py`의
`extract` 명령이 이렇게 한다). `PostgresStore`와 마찬가지로 완전히 opt-in이며(`store`와
독립적으로 켜고 끌 수 있음), 켜지 않으면 아무 파일도 만들지 않는다.

## 사용법

```bash
uv run knowledge-forge extract tests/fixtures/sample.md --okf-dir ./okf --no-store
```

`--store`(기본값)와 동시에 켜서 Postgres + OKF 파일 트리를 둘 다 만들 수도 있다.

## 후속 작업

- 대용량 Knowledge Base에서 `manifest.yaml`/`views/*.yaml`을 매번 전체 스캔으로 재생성하는
  비용이 문제가 되면, 증분(변경분만) 갱신으로 바꿀 수 있다 — 지금은 정확성을 단순성보다
  우선했다.
- `views/by_document.yaml`은 Project의 대용이다 — 스키마에 별도 Project 개념이 생기면
  `by_document`와 별개로 `by_project.yaml`을 추가하면 된다(레코드 포맷 변경 불필요).
