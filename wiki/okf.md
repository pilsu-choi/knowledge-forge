# OKF — 파일 기반 Knowledge Base

`knowledge-forge extract --okf-dir <dir>`, `knowledge-forge schedule --okf-dir <dir>`,
또는 `POST /ingest`의 `okf_dir` 필드(`serve`/`worker` job queue 경로, [`api.md`](api.md))
켜면, `PostgresStore`(선택)와는 독립적으로 같은 파이프라인 산출물을 파일 트리로도 기록한다.
Postgres 없이도(또는 Postgres와 나란히) 사람이 읽고 git으로 diff할 수 있는 Knowledge Base가
필요하다는 문제의식에서 나왔다 — 자세한 배경은 [`architecture.md`](architecture.md)의
"Hierarchy는 Knowledge Base의 유일한 조직 원리가 아니다" 절 참고.

## 왜 필요한가, 그리고 왜 실제 OKF 스펙을 따르는가

`PostgresStore`는 질의에 강하지만(조인, JSONB 필터) 데이터베이스가 있어야만 접근할 수 있고,
레코드 하나를 눈으로 확인하려면 `psql`을 열어야 한다. OKF는 그 반대 극단을 채운다:
데이터베이스 없이도 존재하고, 파일 하나 = 레코드 하나라 grep/diff/git으로 다룰 수 있다.

이전 버전(v1)은 이름만 OKF를 빌리고 실제로는 순수 YAML 레코드 트리(파일명은 opaque한
UUID, 관계는 별도 엣지 파일, progressive disclosure 없음)였다 - `PostgresStore`/Graph DB로
넘어가기 전의 "소스 오브 트루스" 백업 정도의 역할이었다. 지금은 그 반대를 실험한다: **DB
없이 이 파일 트리만으로 에이전트가 컨텍스트를 구성하고 검색 task를 수행**하는 것이 목표라서,
실제 [Open Knowledge Format v0.2 스펙](open-knowledge-format.md)을 그대로 따른다 - markdown +
YAML frontmatter, `index.md`/`log.md`를 통한 progressive disclosure, 사람이 읽는 파일명. 이
전환이 막고 있던 로드맵의 "제외 범위" 전제(Search/RAG는 나중에 인덱스를 얹으면 된다)는
[`roadmap.md`](roadmap.md)에 갱신해 뒀다.

## 디렉터리 레이아웃

```text
<okf-dir>/{project_id}/          # 프로젝트 하나 = OKF 번들 하나
  index.md                       # 번들 루트, progressive disclosure (스펙 S8), okf_version 프론트매터
  log.md                         # extracted_at 기준 시간순 변경 이력 (스펙 S9)
  manifest.yaml                  # id -> path 가속 인덱스. 지식 저장소가 아님 (아래 절 참고)
  resources/{slug}-{source_id8}.md   # Source + Document 하나 = concept 하나
  resources/index.md
  objects/{type}/{slug}-{ko_id8}.md  # KnowledgeObject 하나 = concept 하나
  objects/{type}/index.md
  relations/{relation_id}.md     # 양쪽 끝을 못 찾은 orphan Relation만 (드묾, 아래 절 참고)
  links/{link_id}.md             # 양쪽 끝을 못 찾은 orphan Link만 (드묾)
  views/by-topic.md              # topic -> concept 목록 (tags를 스캔해 만든 캐시)
```

- **concept 파일명은 `{slug}-{id8}.md`**, 순수 id가 아니다. 에이전트가 grep하기 전에
  `ls`/glob만으로 디렉터리 안에 뭐가 있는지 감을 잡을 수 있어야 한다는 게 이 실험의 핵심이라,
  한국어를 포함한 원문 텍스트를 그대로 슬러그로 쓴다(로마자 표기로 바꾸지 않는다 - 코퍼스가
  이미 grep할 언어 그대로 남는 게 검색에 유리하다).
- **`resources/`**: `Source` 하나당 concept 파일 하나. frontmatter의 `type`은
  `Slack Source`/`Gmail Source`처럼 소스 종류를 스펙의 `type` 필드로 승격한 것이고,
  `resource`는 원본 URI. body의 "## 추출된 개념" 섹션은 `write_views()`가 매번
  `objects/`를 스캔해 그 리소스에서 뽑힌 KnowledgeObject 링크로 다시 채운다 - Project의
  대용이던 옛 `by_document.yaml`이 여기로 흡수됐다.
- **`objects/{type}/`**: `KnowledgeObject.type`별 하위 디렉터리에 concept 파일을 하나씩
  저장한다. frontmatter는 스펙 필드(`type`/`title`/`description`/`resource`/`tags`/
  `generated`/`status`/`sources`)와 knowledge-forge 전용 확장 필드
  (`knowledge_object_id`/`evidence`/`model_used`/`version`/`supersedes`/`source_hash`/
  `content_at`)를 같이 담는다(스펙 S4.1이 producer-defined 키를 명시적으로 허용). body는
  `content`를 그대로 덤프하지 않고 사람이 읽는 문장으로 렌더링한다 - grep 재현율은
  `key: value` YAML보다 자연어 문장에서 훨씬 좋다.
  - `content_at`은 `generated.at`(이 파일을 파이프라인이 만든 시각)과 다르다 - 그 KO가 속한
    리소스의 `modified_at`(없으면 `collected_at`)을 그대로 옮겨 적은, "실제로 언제 있었던
    일인지"에 가까운 시각이다. `search/service.py`의 `file_search` 시간 필터(`time_range`,
    wiki/search-architecture.md "⑥ Temporal Search")가 이 필드를 우선으로 본다 - 배치로 오래된
    이력을 한꺼번에 추출하면 `generated.at`은 전부 그 배치를 돌린 하루로 뭉치기 때문에, "지난달"
    같은 질문에는 `generated.at`이 아니라 `content_at`이 맞는 기준이다.
- **`views/`**: `by-topic.md`만 남았다. `by_document.yaml`은 리소스 concept의 body로,
  `timeline.yaml`은 프로젝트 루트 `log.md`(스펙이 이미 이 용도로 정의해 둔 파일)로
  대체됐다. `by-topic.md`도 스펙 S3.1이 "별도 포맷 없이 tags를 스캔해 즉석 합성하라"고
  말하는 것의 캐시일 뿐 - 정본은 각 concept 파일의 `tags` 프론트매터다.

## Relation/Link: 왜 더 이상 별도 엣지 파일이 기본이 아닌가

OKF v0.2에는 타입 있는 엣지 레코드가 없다(스펙 S6.1 - 관계는 마크다운 링크 + 프로즈로만
표현된다). 하지만 `Relation`/`Link`가 들고 있는 `type`/`confidence`/`label`을 프로즈로
뭉개면 질의 불가능한 정보가 된다. 그래서 `write_relations()`/`write_links()`는 별도 파일을
새로 만드는 대신 **양쪽 endpoint concept 파일에 엣지를 인라인**한다:

```yaml
relations:                     # objects/{type}/{...}.md의 frontmatter 확장 필드
  - relation_id: rel_...
    type: mentions
    direction: out              # subject 쪽엔 out, object 쪽엔 in
    peer: /objects/entity/농협은행-6748389c.md
    peer_title: 농협은행
    label: null
    confidence: null
```

같은 정보가 body의 `## Relations` 섹션에 `- mentions → [농협은행](...)` 형태로도
렌더링된다(스펙 S6.1 그대로) - frontmatter가 정본이고 body 섹션은 매번 그 리스트에서
다시 그려지는 파생물이라, `write_relations()`를 여러 번 불러도(증분 동기화) 중복 append 없이
멱등하다. `Link`도 `resources/*.md`에 같은 방식(`links:` 프론트매터, `## Links` 섹션)으로
인라인된다.

이렇게 하면 관계 하나의 의미를 알기 위해 relation 파일 → subject 파일 → object 파일 3개를
열어야 했던 v1의 문제가 사라진다: concept 파일 하나만 열어도 "이게 뭘 언급하는지"와
"누가 이걸 언급하는지"가 바로 보인다.

subject/object(또는 source_ref/target_ref)가 가리키는 concept이 디스크에서 아직 안 보이면
(있어서는 안 되지만 대비는 해 둔 경우 - 예: 관계가 참조하는 객체가 다른 프로젝트/다른 실행에
있어 아직 안 써진 경우) `relations/{relation_id}.md` / `links/{link_id}.md`에 orphan 레코드로
떨어진다. 데이터가 조용히 유실되지 않게 하는 안전망일 뿐, 정상 경로에서는 이 두 디렉터리가
거의 항상 비어 있다.

## `manifest.yaml`은 여전히 카탈로그이지 Knowledge 저장소가 아니다

`manifest.yaml`은 각 레코드의 **id + type + 상대 경로**만 담는 인덱스로, `.md` 확장자를
쓰지 않는다 - concept이 아니라 순수 가속 인덱스이므로 스펙의 "모든 `.md` 파일은 frontmatter를
가져야 한다"는 conformance 요건과 무관하다.

```yaml
generated_at: '2026-09-01T12:00:00+00:00'
pipeline_version: 0.1.0
resources:
  - {id: src_..., type: slack, path: resources/농협은행-킥오프-246fb458.md}
objects:
  - {id: ko_..., type: decision, path: objects/decision/....md}
relations: []   # orphan만 카탈로그된다 - 정상 경로에서는 비어 있음
links: []
```

전체 knowledge 내용(요약, content, evidence, relations/links 등)은 각 concept 파일에만
있다. `objects`/`resources`/`by-topic.md`/`log.md`/`index.md` 전부 `write_manifest()`/
`write_views()`가 **트리를 스캔해서 매번 다시 생성**한다(누적된 in-memory 상태가 아니다) -
그래서 여러 번의 파이프라인 실행(예: `knowledge-forge schedule`의 증분 동기화)이 같은
`--okf-dir`에 쓰더라도, 마지막 실행 한 번의 산출물만 남는 대신 트리에 쌓인 모든 레코드가
항상 정확히 반영된다.

## `OKFWriter` (`src/knowledge_forge/okf/writer.py`)

```python
writer = OKFWriter(root)
writer.write_resource(source, document)              # Source당 1회
writer.write_objects(outcome.objects)                # 문서당 (병합된 ExtractionOutcome 1개)
writer.write_relations(outcome.relations, project_id) # 문서당 - 위 objects가 먼저 써져 있어야 인라인된다
writer.write_manifest()                              # 프로젝트별로 트리 스캔 후 재생성
writer.write_views()                                 # 프로젝트별로 트리 스캔 후 재생성
```

메서드 이름/시그니처는 v1과 동일하게 유지했다(`pipeline.py`/`cli.py`/`ingest.py`/
`scheduling/scheduler.py`의 호출부는 변경 없음) - 바뀐 건 각 메서드가 디스크에 쓰는 파일
포맷뿐이다. `write_relations()`가 인라인에 성공하려면 subject/object가 **이미 디스크에
있어야** 한다 - `OKFWriter`는 같은 인스턴스가 이번 실행에서 쓴 객체를 메모리 인덱스로 들고
있고, 미스가 나면 `objects/**/*.md`를 한 번 스캔해 캐시한다(증분 동기화로 다른 실행이 이미
써 둔 객체도 이렇게 찾는다). `pipeline.py`/`cli.py`의 `db export-okf`는 항상 모든 객체를
먼저 쓰고 나서 관계를 쓰므로 이 순서 요구사항을 자연히 만족한다.

**다중 프로젝트 라우팅**, **`supersedes` 처리**(재추출로 대체된 객체는 `status: deprecated`로
패치되고 새 concept으로의 링크가 body에 남는다 - 스펙 S5.4), **증분 동기화 안전성**은 v1과
같은 원칙을 따른다 - 자세한 라우팅 규칙은 `writer.py`의 각 메서드 docstring 참고.

## 사용법

```bash
uv run knowledge-forge extract tests/fixtures/sample.md --okf-dir ./okf --no-store
uv run knowledge-forge schedule tests/fixtures --okf-dir ./okf --no-store --interval-seconds 600

# serve/worker job queue - POST /ingest에 okf_dir을 주면 그 job도 같은 트리에 쓴다
curl -X POST localhost:8000/ingest -H 'Content-Type: application/json' \
  -d '{"path": "/app/input/sample.md", "okf_dir": "/app/okf"}'

# Postgres에 이미 있는 데이터로부터 새 포맷의 OKF 트리를 다시 만든다
uv run knowledge-forge db export-okf --okf-dir ./okf
```

`--store`(기본값)와 동시에 켜서 Postgres + OKF 파일 트리를 둘 다 만들 수도 있다.

## 브라우저로 확인하기 (`GET /ui`)

`/ui`는 `frontend/`의 React+Vite SPA다(`GET /okf/*` 위에서 동작, [`api.md`](api.md)는
파일시스템만 읽으므로 Postgres/Celery/Redis 설정 없이도 뜬다). 한 번 빌드해 두면
`api/app.py`가 `frontend/dist`를 `/ui`에 그대로 마운트한다(디렉터리가 없으면 `/ui`
마운트만 건너뛰고 나머지 API는 정상 기동 - 아직 빌드 전인 새 클론에서도 서버가 죽지 않는다):

```bash
npm --prefix frontend install
npm --prefix frontend run build     # frontend/dist 생성 (1회, 이후 코드 바뀔 때만 재실행)

KF_OKF_DIR=./okf uv run knowledge-forge serve --port 8000
# http://localhost:8000/ui/ 접속 - 프로젝트 선택 → 타입별 카드 목록 → 클릭해 frontmatter +
# 렌더링된 body(Relations 링크는 클릭으로 peer 오브젝트 이동) 확인. ⌘K/Ctrl+K로 프로젝트
# 전체 오브젝트 검색.
```

프론트엔드 자체를 고칠 때는 핫리로드가 붙는 Vite 개발 서버를 따로 띄운다(`vite.config.ts`가
`/okf/*`를 8000번 포트로 프록시한다 - 백엔드는 위 `serve` 그대로 켜 둔 채):

```bash
npm --prefix frontend run dev   # http://localhost:5173
```

좌측 레일의 "그래프" 탭은 같은 트리를 Cytoscape.js로 그린다 - 문서(`resources/`)를 compound
parent로, 그 안에서 추출된 KnowledgeObject를 자식 노드로, `relations:` 인라인 필드를 엣지로
그린다(`GET /okf/projects/{id}/graph`). 프로젝트가 커도(오브젝트 수백 개) 첫 화면은 문서 단위로
접힌 채 시작하고, 문서별 +/-나 "모두 펼치기"로 드릴다운한다.
