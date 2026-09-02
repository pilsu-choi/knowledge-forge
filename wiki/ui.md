# `/ui` — React+Vite 기반 OKF 뷰어

`frontend/`에 있는 React+Vite SPA로, `api/app.py`가 `frontend/dist`를 `/ui`에 마운트해서
서빙한다. [`okf.md`](okf.md)의 `GET /okf/*`만 호출하는 순수 프론트엔드라 Postgres/Celery/
Redis 없이도 뜬다. 빌드/개발 서버 실행 방법은 [`okf.md`의 "브라우저로 확인하기"
절](okf.md#브라우저로-확인하기-get-ui)을 참고 — 이 문서는 왜 이렇게 만들었는지와 구조를 다룬다.

이전 버전(손으로 짠 598줄짜리 단일 vanilla JS `index.html`)을 완전히 대체했다. 기능은
동일하다 — 프로젝트 선택 → 타입별 개요/카드/검색 → 파일 상세(frontmatter + 렌더링된 body)
→ 그래프 — 여기에 전역 검색(⌘K)이 추가됐다.

## 비주얼 방향 — Obsidian 느낌의 지식 관리 툴

Tailwind + shadcn/ui(Radix 프리미티브 + `class-variance-authority`) 위에, Obsidian의
UI 관용구를 참고한 다크 우선 팔레트를 얹었다:

- **`styles/index.css`가 shadcn 변수 컨벤션**(`--background`/`--foreground`/`--card`/
  `--primary`/`--border`/`--ring` 등 + 사이드바 전용 `--sidebar-*` 그룹)**을 그대로 쓴다** -
  다크가 기본(`.dark` 클래스, Obsidian 자체가 다크 기본이라는 것과 동일한 선택), 라이트는
  완전히 별도로 정의된 값(미완성 fallback이 아님). 테마 전환은 `lib/theme.ts`가 "system"도
  포함해 매번 JS에서 라이트/다크를 확정하고 `<html>`에 `dark` 클래스를 토글하는 방식이라
  (`@custom-variant dark (&:is(.dark *))`), Tailwind의 `dark:` variant가 클래스 기반으로
  일관되게 동작한다.
- **보라색 accent(`--primary`)** - Obsidian의 시그니처 accent 컬러 계열.
- **KnowledgeObject 9개 타입 + Source 타입 색(`--t-*`/`--doc-*`, `lib/graphPalette.ts`)**은
  채도를 낮춘 파스텔에 가까운 톤으로 잡았다 - Obsidian 그래프 뷰가 그룹을 칠하는 방식과
  비슷하게, 카드/배지/그래프 어디서나 화려한 대시보드보다는 차분한 "성좌" 느낌을 내려는
  선택.
- **타이포는 Inter(본문/UI) + JetBrains Mono(id/경로/코드 전용 유틸리티)** - 이전의
  IBM Plex Mono 헤딩 중심 "터미널" 정체성은 걷어내고, 노트 읽기 도구에 맞는 조용한 본문
  타이포로 바꿨다.
- **경로 표시줄(`components/PathBar.tsx`)**은 유지 - `okf://{project}/...` 브레드크럼이
  세그먼트별로 클릭 가능한 채, 톤만 Obsidian 사이드바에 어울리게 muted하게 낮췄다.
- **frontmatter는 Obsidian의 "Properties" 위젯처럼** 렌더링한다(`FrontmatterPanel.tsx`) -
  옆 칼럼이 아니라 제목 바로 아래, 본문과 같은 폭의 카드 하나로. 원래 파일 상세 페이지가
  `grid-template-columns: minmax(220px,300px) 1fr`인 2단 레이아웃이었는데, `log.md`/
  `index.md`처럼 frontmatter가 거의 없는 파일은 두 번째(넓은) 칸이 아니라 **첫 번째(좁은)
  칸에 자식이 자동 배치**되면서 본문이 좁은 칸에 몰리고 나머지가 텅 비는 버그가 있었다 -
  단일 칼럼(`max-width: 760px`, `FileDetailPage.tsx`)으로 바꾸면서 근본적으로 없앴다.
- **그래프(`GraphCanvas.tsx`)**도 같은 방향 - 오브젝트 노드를 22px 색칠 원 대신 9px
  민무늬 점으로, 엣지는 화살표 없는 얇은 선으로 바꿔 Obsidian 그래프 뷰의 "성좌" 느낌에
  가깝게 했다. 노드 클릭 시 그 1-hop 이웃으로 자동 줌/센터하는 동작, 줌 인/아웃/전체보기
  버튼, 드로어에서 `GET /okf/.../file/...`을 즉시 불러와 `FrontmatterPanel`로 전체 속성을
  보여주는 것은 그대로.
- **⌘K 커맨드 팔레트(`components/CommandPalette.tsx`, `cmdk` + shadcn 스타일링)** - 기존엔
  타입 카드 목록 안에서만 되던 client-side 필터를 프로젝트 전역 검색으로 격상. 새 백엔드
  엔드포인트 없이 `GET /okf/projects/{id}/objects/{type}`을 매니페스트의 모든 타입에 대해
  프리페치해서 클라이언트에서 매칭한다(`hooks/useSearchIndex.ts`) - OKF 트리가 "오브젝트
  수백 개" 규모라 전량 프리페치가 무리 없다는 전제.

shadcn 프리미티브는 CLI로 생성하지 않고 `components/ui/{button,badge,separator}.tsx`에
표준 레시피 그대로 손으로 추가했다(이 프로젝트가 실제로 쓰는 컴포넌트만) - CLI(`npx shadcn
init`)가 이 리포의 비TTY 실행 환경에서 매번 대화형 프롬프트에 걸려 막혔던 것도 있고, 어차피
팔레트를 통째로 새로 잡을 거라 생성기가 주는 이점이 크지 않았다.

## 디렉터리 구조

```text
frontend/src/
├── lib/            api.ts(타입+fetch 래퍼, okf/reader.py 반환 shape과 1:1 대응)
│                   theme.ts, graphPalette.ts, typeMeta.ts, pathSegments.tsx, utils.ts(cn())
├── hooks/          useOkf.ts(React Query 훅), useSearchIndex.ts, useResolvedTheme.ts
├── components/
│   ├── ui/         button.tsx, badge.tsx, separator.tsx (shadcn 표준 레시피, 손으로 추가)
│   └── TopBar, PathBar, Rail, CardGrid, TypeBadge, FrontmatterPanel, MarkdownBody,
│       GraphCanvas, CommandPalette, ThemeToggle, StateMessage
├── routes/         ProjectPickerPage, ProjectLayout, OverviewPage, TypeListPage,
│                   ResourcesPage, GraphPage(지연 로드), FileDetailPage
├── types/          cytoscape-expand-collapse.d.ts, cytoscape-core-augment.d.ts
│                   (둘 다 타입이 없는/부족한 패키지용 - 아래 "함정" 참고)
└── styles/index.css  shadcn 변수 + Obsidian 톤 팔레트(라이트/다크) + Cytoscape가 캔버스에
                       그리느라 Tailwind로 못 건드리는 자리만 최소한의 손 CSS로.
```

`@/*`가 `src/*`로 매핑돼 있다(`vite.config.ts`의 `resolve.alias`, `tsconfig.app.json`의
`compilerOptions.paths`) - shadcn 컨벤션과 동일해서 나중에 CLI로 컴포넌트를 더 추가해도
그대로 맞는다.

**마크다운은 프론트에서 다시 파싱하지 않는다** — `GET /okf/.../file/...`이 이미 `body_html`을
서버에서 렌더링해 내려주므로(`okf/reader.py:read_markdown_file`, markdown-it) `MarkdownBody`는
`dangerouslySetInnerHTML`로 그대로 꽂고, 내부 `.md` 링크 클릭만 가로채 클라이언트 라우팅한다.

## 알려진 함정 (다음에 또 안 만들려고 적어둠)

- **`cytoscape-expand-collapse`의 오버레이 캔버스는 `z-index: 999`가 하드코딩돼 있다.**
  그래프 위에 뜨는 오버레이 UI(드로어 등)는 반드시 이보다 높은 `z-index`를 명시해야 한다 —
  안 그러면 그 캔버스가 시각적으로도, 클릭/휠 히트테스트에서도 위에 있는 걸 이겨버려서
  드로어 안의 스크롤·버튼이 전부 먹통이 된다(`GraphCanvas.tsx`의 드로어 `z-[1000]` 클래스
  주석 참고). CSS 스택 순서만 봐서는 절대 안 보이는 버그라 실제 브라우저(또는 헤드리스
  브라우저)로 클릭/스크롤을 재현해봐야 잡힌다 — devtools의 elementFromPoint가 제일 빠른
  진단 수단이었다.
- **`flex:1; overflow-y:auto`인 flex 자식은 `min-height: 0`도 같이 줘야 한다.** 안 그러면
  flex item의 기본 `min-height:auto`가 컨텐츠 높이만큼 자라버려서, 스크롤바 대신 부모 밖으로
  내용이 넘쳐버린다(그래프 드로어 body).
- **CSS Grid에 자식이 하나뿐이면 정의된 첫 번째 트랙에 들어간다.** `grid-template-columns:
  minmax(220px,300px) 1fr`로 2단을 정의해 놓고 첫 번째 칸(frontmatter 패널)이 조건부로
  안 그려지면, 남은 자식(본문)이 넓은 `1fr` 칸이 아니라 좁은 첫 번째 칸에 자동 배치된다 -
  "본문이 좁은 칸에 몰려 있고 나머지 공간이 텅 비어 있다"는 증상으로 나타난다. 조건부로
  없어질 수 있는 그리드 자식이 있다면 애초에 그리드를 안 쓰거나(이번 수정처럼) 명시적으로
  `grid-column`을 지정해야 한다.
- **`cytoscape-expand-collapse`엔 타입 정의가 없다.** `src/types/cytoscape-expand-collapse.d.ts`가
  최소 타입을 선언하는데, 이 파일은 반드시 **top-level import/export가 없는 순수 global
  스크립트**여야 한다 — `import`가 하나라도 있으면 그 파일이 "모듈"이 되면서
  `declare module "x" {}`가 새 모듈 선언이 아니라 (이미 타입이 있는 모듈에 대한) **augmentation
  시도**로 해석돼 조용히 무시된다. `cytoscape` 자체에 대한 augmentation(`Core.expandCollapse`
  메서드 추가)은 반대로 `cytoscape-core-augment.d.ts`처럼 import가 있는 일반 모듈 파일 안에서
  해야 한다 — `cytoscape`는 실제 타입이 있으니 augmentation이 정상 동작한다.
- **`StaticFiles(html=True)`는 SPA 클라이언트 라우팅과 그대로 안 맞는다.** `/ui/index.html`
  요청이나 `/ui/`(디렉터리) 요청은 알아서 index.html을 주지만, `/ui/{project}/graph`처럼
  react-router가 만드는 나머지 경로는 실제 파일이 없으니 그냥 404가 난다 - 브라우저
  새로고침이나 링크를 붙여넣었을 때만 터지는 문제라 첫 개발 중엔 안 보이기 쉽다.
  `api/app.py`의 `_SPAStaticFiles`가 404를 잡아 `index.html`로 폴백해서 고쳤다.
- **빌드 산출물(`frontend/dist`)은 커밋하지 않는다** — `.gitignore`의 `dist/`가 이미 커버.
  로컬에서 `/ui`를 띄우려면 `npm --prefix frontend run build`를 먼저 돌려야 하고,
  `api/app.py`는 그 디렉터리가 없으면 `/ui` 마운트만 건너뛰도록 가드돼 있다(나머지 API는
  정상 기동). Docker 이미지는 `Dockerfile`의 별도 Node 빌드 스테이지가 대신 해준다.
