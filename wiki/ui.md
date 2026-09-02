# `/ui` — React+Vite 기반 OKF 뷰어

`frontend/`에 있는 React+Vite SPA로, `api/app.py`가 `frontend/dist`를 `/ui`에 마운트해서
서빙한다. [`okf.md`](okf.md)의 `GET /okf/*`만 호출하는 순수 프론트엔드라 Postgres/Celery/
Redis 없이도 뜬다. 빌드/개발 서버 실행 방법은 [`okf.md`의 "브라우저로 확인하기"
절](okf.md#브라우저로-확인하기-get-ui)을 참고 — 이 문서는 왜 이렇게 만들었는지와 구조를 다룬다.

이전 버전(손으로 짠 598줄짜리 단일 vanilla JS `index.html`)을 완전히 대체했다. 기능은
동일하다 — 프로젝트 선택 → 타입별 개요/카드/검색 → 파일 상세(frontmatter + 렌더링된 body)
→ 그래프 — 여기에 전역 검색(⌘K)이 추가됐다.

## 왜 이런 비주얼인가 — "Forge" 아이덴티티

제품명이 "Knowledge **Forge**"이고, OKF 자체가 "raw한 Source/Document가 타입 있는
KnowledgeObject로 정제되어 grep/git-diff 가능한 파일 트리로 남는다"는 파이프라인이라
([`okf.md`](okf.md)), 이 raw→typed 전환을 색 체계의 논리로 그대로 가져왔다:

- **ember(주황) = raw** — Source/Document(`resources/`) 계열. `--doc-slack`/`--doc-gmail`/
  `--doc-other`.
- **steel(청록/파랑) = refined** — 추출된 KnowledgeObject 계열. 9개 타입(`entity`/`fact`/
  `concept`/`decision`/`event`/`issue`/`action_item`/`policy`/`requirement`)이 steel
  계열에서 갈라져 나온 보석톤을 하나씩 갖는다 (`lib/graphPalette.ts`, `styles/index.css`의
  `--t-*` 변수).
- **헤딩은 IBM Plex Mono** — "grep 가능한 파일명" 정체성을 타이포에 새김. 본문은 IBM Plex
  Sans.
- **경로 표시줄(`components/PathBar.tsx`)** — 상단바에 실제 파일 경로처럼 보이는
  `okf://{project}/...` 브레드크럼을 두고 세그먼트를 클릭 가능하게 했다. "사람이 읽는
  파일명이 곧 검색 표면"이라는 OKF 설계 철학을 내비게이션 자체로 드러내는 장치.
- **육각형 "hallmark" 배지(`components/TypeBadge.tsx`)** — 타입 표시가 나오는 모든 곳(카드,
  좌측 레일, 그래프 드로어, ⌘K 결과)에서 공통으로 쓰는 시각 모티프.
- **⌘K 커맨드 팔레트(`components/CommandPalette.tsx`, `cmdk`)** — 기존엔 타입 카드 목록
  안에서만 되던 client-side 필터를 프로젝트 전역 검색으로 격상. 새 백엔드 엔드포인트 없이
  `GET /okf/projects/{id}/objects/{type}`을 매니페스트의 모든 타입에 대해 프리페치해서
  클라이언트에서 매칭한다(`hooks/useSearchIndex.ts`) — OKF 트리가 "오브젝트 수백 개" 규모라
  전량 프리페치가 무리 없다는 전제.

## 디렉터리 구조

```text
frontend/src/
├── lib/            api.ts(타입+fetch 래퍼, okf/reader.py 반환 shape과 1:1 대응)
│                   theme.ts, graphPalette.ts, typeMeta.ts, pathSegments.tsx
├── hooks/          useOkf.ts(React Query 훅), useSearchIndex.ts, useResolvedTheme.ts
├── components/     TopBar, PathBar, Rail, CardGrid, TypeBadge, FrontmatterPanel,
│                   MarkdownBody, GraphCanvas, CommandPalette, ThemeToggle
├── routes/         ProjectPickerPage, ProjectLayout, OverviewPage, TypeListPage,
│                   ResourcesPage, GraphPage(지연 로드), FileDetailPage
├── types/          cytoscape-expand-collapse.d.ts, cytoscape-core-augment.d.ts
│                   (둘 다 타입이 없는/부족한 패키지용 - 아래 "함정" 참고)
└── styles/index.css  Forge 토큰(라이트/다크) + 컴포넌트 클래스. Tailwind는 레이아웃
                       유틸리티 용도로만 깔아뒀고, 시그니처 요소는 전부 이 파일의 손으로
                       짠 클래스로 스타일링했다(shadcn 등 기성 킷 미사용).
```

**마크다운은 프론트에서 다시 파싱하지 않는다** — `GET /okf/.../file/...`이 이미 `body_html`을
서버에서 렌더링해 내려주므로(`okf/reader.py:read_markdown_file`, markdown-it) `MarkdownBody`는
`dangerouslySetInnerHTML`로 그대로 꽂고, 내부 `.md` 링크 클릭만 가로채 클라이언트 라우팅한다.

**그래프(`GraphCanvas.tsx`)**는 Cytoscape.js + `cytoscape-expand-collapse`를 그대로 포팅했다
— `GET /okf/.../graph`의 문서(compound parent)/오브젝트(자식)/엣지 구조, 접기 상태로 시작해
드릴다운하는 동작 전부 기존 vanilla 구현과 동일하다. 새로 추가된 것: 노드 클릭 시 그 노드의
1-hop 이웃으로 자동 줌/센터(`cy.animate({fit:...})`), 줌 인/아웃/전체보기 버튼, 드로어에서
`GET /okf/.../file/...`을 즉시 불러와 `FrontmatterPanel`로 전체 속성 표시.

## 알려진 함정 (다음에 또 안 만들려고 적어둠)

- **`cytoscape-expand-collapse`의 오버레이 캔버스는 `z-index: 999`가 하드코딩돼 있다.**
  그래프 위에 뜨는 오버레이 UI(드로어 등)는 반드시 이보다 높은 `z-index`를 명시해야 한다 —
  안 그러면 그 캔버스가 시각적으로도, 클릭/휠 히트테스트에서도 위에 있는 걸 이겨버려서
  드로어 안의 스크롤·버튼이 전부 먹통이 된다(`styles/index.css`의 `.g-drawer { z-index:
  1000; }` 주석 참고). CSS 스택 순서만 봐서는 절대 안 보이는 버그라 실제 브라우저(또는
  헤드리스 브라우저)로 클릭/스크롤을 재현해봐야 잡힌다 — devtools의 elementFromPoint가
  제일 빠른 진단 수단이었다.
- **`flex:1; overflow-y:auto`인 flex 자식은 `min-height: 0`도 같이 줘야 한다.** 안 그러면
  flex item의 기본 `min-height:auto`가 컨텐츠 높이만큼 자라버려서, 스크롤바 대신 부모 밖으로
  내용이 넘쳐버린다(`.g-drawer-body`).
- **`cytoscape-expand-collapse`엔 타입 정의가 없다.** `src/types/cytoscape-expand-collapse.d.ts`가
  최소 타입을 선언하는데, 이 파일은 반드시 **top-level import/export가 없는 순수 global
  스크립트**여야 한다 — `import`가 하나라도 있으면 그 파일이 "모듈"이 되면서
  `declare module "x" {}`가 새 모듈 선언이 아니라 (이미 타입이 있는 모듈에 대한) **augmentation
  시도**로 해석돼 조용히 무시된다. `cytoscape` 자체에 대한 augmentation(`Core.expandCollapse`
  메서드 추가)은 반대로 `cytoscape-core-augment.d.ts`처럼 import가 있는 일반 모듈 파일 안에서
  해야 한다 — `cytoscape`는 실제 타입이 있으니 augmentation이 정상 동작한다.
- **빌드 산출물(`frontend/dist`)은 커밋하지 않는다** — `.gitignore`의 `dist/`가 이미 커버.
  로컬에서 `/ui`를 띄우려면 `npm --prefix frontend run build`를 먼저 돌려야 하고,
  `api/app.py`는 그 디렉터리가 없으면 `/ui` 마운트만 건너뛰도록 가드돼 있다(나머지 API는
  정상 기동). Docker 이미지는 `Dockerfile`의 별도 Node 빌드 스테이지가 대신 해준다.
