# OKF — 파일 기반 Knowledge Base

`knowledge-forge extract --okf-dir <dir>` 또는 `knowledge-forge schedule --okf-dir <dir>`을
켜면, `PostgresStore`(선택)와는 독립적으로 같은 파이프라인 산출물을 파일 트리로도 기록한다.
Postgres 없이도(또는 Postgres와 나란히) 사람이 읽고 git으로 diff할 수 있는 Knowledge Base가
필요하다는 문제의식에서 나왔다 — 자세한 배경은 [`architecture.md`](architecture.md)의
"Hierarchy는 Knowledge Base의 유일한 조직 원리가 아니다" 절 참고. `serve`/`worker` job queue
경로에는 아직 연결되어 있지 않다 — [`api.md`](api.md) 참고.

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
writer.write_resource(source, document)              # Source당 1회
writer.write_objects(outcome.objects)                # 문서당 (병합된 ExtractionOutcome 1개)
writer.write_relations(outcome.relations, project_id) # 문서당
writer.write_manifest()                              # 프로젝트별로 트리 스캔 후 재생성
writer.write_views()                                 # 프로젝트별로 트리 스캔 후 재생성
```

`pipeline.run_pipeline(..., okf_writer=writer)`를 넘기면 위 `write_resource`/`write_objects`/
`write_relations` 호출을 파이프라인이 알아서 각 단계에서 수행한다 — `write_manifest()`/
`write_views()`는 호출자가 직접 부른다. 언제 부르느냐는 실행 모델에 따라 다르다: `cli.py`의
`extract`(1회성)는 전체 실행이 끝난 뒤 딱 한 번(`cli.py:148-150`), `schedule`(장기 실행,
반복 tick)은 **매 tick이 끝날 때마다**(`scheduling/scheduler.py`의 `build_tick`) 다시
부른다 — 트리 스캔 기반 재생성이라 반복 호출해도 안전하고(위 "카탈로그" 절), 그래야 스케줄러가
증분 동기화로 새로 수집한 것들이 다음 tick을 기다리지 않고 그 tick이 끝나는 즉시 매니페스트/뷰에
반영된다. `PostgresStore`와 마찬가지로 완전히 opt-in이며(`store`와 독립적으로 켜고 끌 수 있음),
켜지 않으면 아무 파일도 만들지 않는다. `serve`/`worker` job queue 경로는 아직 `okf_writer`를
전혀 연결하지 않는다.

**다중 프로젝트 라우팅**: `OKFWriter(root, project_id="default")`의 `project_id`는 더 이상
전체 인스턴스를 하나의 프로젝트에 고정하지 않는다 — `write_resource`/`write_objects`는 넘겨받은
`Source.project_id`/`KnowledgeObject.source.project_id`를 보고 그때그때 올바른
`<root>/{project_id}/` 아래에 쓴다(예: `KF_SLACK_CHANNEL_PROJECT_MAP`으로 여러 프로젝트의
채널을 한 번에 수집하는 Slack 커넥터, [`saas-connectors.md`](saas-connectors.md) 참고).
`Relation`은 자기 자신의 `Source`가 없어서 이렇게 라우팅할 수 없으므로 `write_relations()`가
`project_id`를 인자로 받는다(`pipeline.py`가 그 문서의 `source.project_id`를 넘겨준다) —
생성자의 `project_id`는 이 인자가 생략됐을 때의 폴백, 그리고 파이프라인이 파일 하나도 쓰기 전
그 프로젝트 서브트리가 최소한 존재하게 만드는 용도로만 남아 있다. `write_manifest()`/
`write_views()`는 이제 그 실행에서 실제로 쓰여진 프로젝트가 몇 개든 `root` 아래에 있는 모든
프로젝트 서브트리를 스캔해 `{project_id: path}`(views는 `{project_id: [path, ...]}`)로 돌려준다
— 프로젝트별 매니페스트/뷰는 여전히 그 프로젝트 자신의 레코드만 담고, 다른 프로젝트와 섞이지
않는다.

## 사용법

```bash
uv run knowledge-forge extract tests/fixtures/sample.md --okf-dir ./okf --no-store
uv run knowledge-forge schedule tests/fixtures --okf-dir ./okf --no-store --interval-seconds 600
```

`--store`(기본값)와 동시에 켜서 Postgres + OKF 파일 트리를 둘 다 만들 수도 있다.

## 그래프 뷰어 대비: 문서 간 링크 설계 결정 (부분 구현)

나중에 Obsidian처럼 OKF 문서 간 관계를 그래프로 시각화하는 UI를 붙일 계획이 있다. 이를 위해
필요한 "문서/리소스 레벨 링크"가 지금 스키마에는 없다는 걸 먼저 확인해야 한다:

- `relations/`는 이미 그래프 엣지 파일이지만 **`KnowledgeObject` 사이(subject_id/object_id)로만
  한정**되고, 그마저도 `extraction/outcome.py`가 한 문서(한 번의 LLM 추출 호출) 범위 안에서만
  만든다 — 다른 Slack 스레드, 다른 채널, 다른 소스 문서를 잇는 엣지는 파이프라인 어디에도 없다.
- `Source`/`Document` 모델에는 `thread_id`/`channel_id`/`parent_id`/`related_ids` 같은 링크
  필드가 전혀 없다. Slack 스레드 부모-답글 관계는 `context/slack.py`의
  `SlackConversationReconstructor`가 **텍스트를 하나의 episode로 병합**하는 방식으로만
  "보존"되고(별도 레코드 간 링크가 아님), `channel`/`thread_ts`는 `Source.metadata`라는 비정형
  dict 안에만 존재해 `views/*.yaml`도 읽지 않는다.

**Obsidian 사전 조사 결과**: Obsidian은 DB 없이 완전히 파일 기반이다. 각 노트 파일 본문에
`[[WikiLink]]`가 인라인으로 박혀 있고, `MetadataCache`가 vault 전체를 한 번 파싱해
`resolvedLinks`(소스 파일 → 대상 파일 인덱스)라는 인메모리 캐시를 만들면 그래프 뷰가 그 캐시를
읽는 방식이다 — 즉 "파일만 파싱하면 그래프가 나온다"는 목표 자체는 우리 방향과 일치하지만,
링크를 **소스 파일 본문에 인라인으로 심는 방식**이라는 점이 다르다.

**결정: B안(별도 엣지 레코드 디렉터리) 채택, A안(인라인 필드) 대신.**

| | A안 (Obsidian 스타일, 인라인) | B안 (채택) |
|---|---|---|
| 방식 | `resources/{id}.yaml`/`objects/{id}.yaml`에 `links: [id, ...]` 필드 추가, 뷰어가 전체 파일을 스캔해 역으로 엣지 조립 | `relations/`와 같은 패턴으로 `links/{link_id}.yaml` 신설 — `source_ref`/`target_ref`/`type`/`created_at`을 담는 독립 레코드 |
| 근거 | Obsidian과 동일한 메커니즘 | 이미 존재하는 `relations/` 패턴과 구조적으로 일관됨. 엣지 자체에 타입/신뢰도 등 메타데이터를 붙이기 쉬움. 레코드 하나 = 파일 하나라는 OKF 전체 원칙(위 "왜 필요한가" 절)과도 맞음 |

구현된 레이아웃:

```
<okf-dir>/{project_id}/
  links/{link_id}.yaml   # Source/Document 레벨 크로스 링크
```

`links/{link_id}.yaml` 형태 (`models/link.py`):

```yaml
link_id: lnk_...
type: thread_reply | same_channel_sequence | thread_sequence | cross_source_reference | duplicate_of
source_ref: {source_id: src_a, document_id: doc_a}
target_ref: {source_id: src_b, document_id: doc_b}
created_at: ...
```

`OKFWriter.write_links()`가 `relations/`와 같은 패턴으로 파일을 쓰고, `manifest.yaml`에도
`links` 카탈로그가 추가된다. `views/graph.yaml`(뷰어용 nodes+edges 통합 뷰)은 아직 없음 —
지금은 뷰어가 `resources/`+`objects/`+`relations/`+`links/`를 직접 조립해야 한다.

**실제로 Link를 생성하는 곳 (파이프라인 배선)**: `ContextReconstructor`가 `RawItem.metadata
["links"]`에 `{"type": ..., "target_uri": <이전에 이미 yield된 아이템의 uri>}` 형태로
힌트를 남기면, `pipeline.run_pipeline`의 `_resolve_links()`가 그 대상의 이미 만들어진
`(source_id, document_id)`를 조회해 실제 `Link` 레코드로 만들고 `okf_writer.write_links()`로
쓴다 — `source_id`/`document_id`가 각각 `stable_id(...)`로 결정론적으로 계산되는 순수
함수라서 가능하다(`ids.py`). 이 컨벤션은 소스 타입에 무관한 일반 배선이고, 실제로 이 메타데이터를
채워 넣는 건 소스별 reconstructor다:

- **Slack** (`context/slack.py`, `SAME_CHANNEL_SEQUENCE`): 같은 채널의 시간순 타임라인
  에피소드끼리만 연속으로 연결한다. 스레드 에피소드(`thread_ts` 있는 것)는 의도적으로 이 체인에
  안 엮는다 — 스레드 yield 순서와 타임라인 순서를 하나로 합치는 문제는 별도 후속 과제로 남김.
- **Gmail** (`context/gmail.py`, `THREAD_SEQUENCE`): Gmail은 Slack의 "채널" 같은 하위
  그룹핑이 없어서, 한 번의 reconstruction이 본 모든 스레드를 각 스레드의 마지막 메시지
  시각 기준으로 전부 하나의 체인으로 잇는다.
- **File/Notion/GDrive**(`NoopReconstructor`), **스레드 답글**(Slack, 이미 텍스트 병합으로
  처리), **크로스 소스 참조**(`CROSS_SOURCE_REFERENCE`), **리소스 레벨 중복**
  (`DUPLICATE_OF`) — 전부 스키마(`LinkType`)만 있고 생성 로직 없음. `dedup.py::merge_duplicates()`
  는 이것과 다른 레이어(같은 문서 안 `KnowledgeObject` 중복 제거)라 혼동 주의.

**롤백 시 참고**: 이 결정이 틀렸다고 판단되면 A안(인라인 `links` 필드)으로 되돌리는 게
대안이다 — 그때는 `links/` 디렉터리와 `pipeline.py`의 `_resolve_links()`/`link_endpoints_by_uri`
캐시를 걷어내고, `resources/`/`objects/` 레코드에 필드만 추가하고 각 `ContextReconstructor`가
그 필드를 직접 채우도록 바꾸면 된다(되돌리는 비용은 낮음).

## 후속 작업

- 대용량 Knowledge Base에서 `manifest.yaml`/`views/*.yaml`을 매번 전체 스캔으로 재생성하는
  비용이 문제가 되면, 증분(변경분만) 갱신으로 바꿀 수 있다 — 지금은 정확성을 단순성보다
  우선했다.
- `views/by_document.yaml`은 Project의 대용이다 — 스키마에 별도 Project 개념이 생기면
  `by_document`와 별개로 `by_project.yaml`을 추가하면 된다(레코드 포맷 변경 불필요).
