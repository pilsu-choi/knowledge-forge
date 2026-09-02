# SaaS 커넥터 (Slack / Gmail / Google Drive / Notion)

`src/knowledge_forge/collectors/saas/` — PRD가 요구한 4개 SaaS 소스 모두 실제 SDK 연동으로
구현되어 있다. 각각 `Collector` 인터페이스(`initial_sync`/`incremental_sync`/`get_cursor`)를
구현하며, `LocalFileCollector`와 동일하게 `RawItem`(원본 바이트 + 메타데이터)만 만든다 — 콘텐츠를
구조화된 `Document`로 바꾸는 건 이후 Parser 단계의 일이라 커넥터는 신경 쓰지 않는다.

## 공통 설계: 클라이언트는 호출자가 주입한다

`SaaSCollector.__init__(self, client, auth_config=None)`은 **이미 인증된 SDK 클라이언트
인스턴스**를 받는다 — 커넥터 코드 자체는 OAuth/토큰 발급을 절대 수행하지 않고, 주입받은
`self._client`의 메서드만 호출한다. 즉 `SlackCollector(client=slack_sdk.WebClient(token=...),
channels=[...])`처럼 **호출자가 클라이언트를 직접 만들어서 넘겨야** 한다.

이는 의도된 스코프 경계다 — 이번 단계에서 구현한 건 "인증된 클라이언트가 주어졌을 때 동기화
로직"이지 "인증 자체"가 아니다. `knowledge-forge slack ...` 같은 소스별 전용 명령은 없지만,
`extract`/`schedule`의 `--source slack` 같은 옵션으로 실행할 수 있다(아래 참고,
[`roadmap.md`](roadmap.md) 1단계).

클라이언트를 직접 만드는 부분은 `collectors/saas/factory.py`의 `build_slack_collector()` /
`build_notion_collector()` / `build_gdrive_collector()` / `build_gmail_collector()`가
대신해준다 — `KF_SLACK_BOT_TOKEN` 등 `.env`의 `KF_*` 설정을 읽어 SDK 클라이언트를 만들고
커넥터까지 조립해서 반환한다(사용법: [`dev-guide.md`](dev-guide.md#saas-커넥터-사용하기)).
`SaaSCollector` 자체의 "클라이언트는 주입받는다"는 설계는 그대로다 — factory는 그 주입을
대신 해주는 얇은 조립 계층일 뿐, 커넥터 인터페이스를 바꾸지 않는다. `knowledge-forge extract`/
`schedule`의 `--source {slack,notion,gdrive,gmail}`가 이 factory를 호출해 CLI에서 바로
실행한다(`src/knowledge_forge/cli.py`의 `_build_collector()`).

Slack은 추가로 `channel_project_map`(채널 ID → project_id)을 받을 수 있다 — 봇 하나가 여러
프로젝트의 채널을 동시에 수집할 때, 채널별로 `RawItem.project_id`를 다르게 태깅해서 파이프라인
전체에 붙는 단일 `--project-id`로는 표현할 수 없는 다대다 귀속을 표현한다
(`KF_SLACK_CHANNEL_PROJECT_MAP`, [`dev-guide.md`](dev-guide.md#saas-커넥터-자격-증명-kf_) 참고).
맵에 없는 채널은 `--project-id`로 폴백한다.

## 커넥터별 요약

| 커넥터 | 초기 동기화 | 증분 동기화 | 삭제 감지 | 커서(`Cursor.data`) |
|---|---|---|---|---|
| Slack | `conversations_history` 페이지네이션 + 스레드는 `conversations_replies` | `conversations_history(oldest=...)` | ❌ 없음(API 한계, 문서화된 제약) | `{"channels": {channel: last_ts}}` |
| Gmail | `users().messages().list`+`.get()` | **History API** `users().history().list(startHistoryId=...)` | ✅ `messagesDeleted` | `{"history_id": ...}` |
| Google Drive | `files().list` + `get_media`/`export_media` | **Changes API** `changes().list(pageToken=...)` | ✅ `change.removed` | `{"start_page_token": ...}` |
| Notion | `search()`/`databases.query()` + 재귀 `blocks.children.list()` | 위와 동일 + `last_edited_time` 필터 | ❌ 없음 → 이전 페이지-id 매니페스트와 diff(`LocalFileCollector`와 동일 원리) | `{"last_synced_at": ..., "known_page_ids": {...}}` |

Gmail/Drive가 각각 History API/Changes API를 쓰는 이유: `q=after:`/`modifiedTime` 같은 단순
필터링은 **삭제를 감지할 수 없다** — 두 API 모두 네이티브 삭제 이벤트(`messagesDeleted`/
`change.removed`)를 제공해 이쪽이 항상 더 정확하다.

## 커넥터별 구현 메모

- **Slack** (`slack.py`, `slack_sdk`, extra `slack`): 스레드 부모 메시지(`reply_count > 0`)는
  `conversations_replies`로 답글까지 수집하되, 그 응답에 부모 메시지 자신도 포함되므로
  중복 방지 처리가 되어 있다. 채널별로 가장 최신 `ts`를 계속 추적해 `get_cursor()`에 반영한다.
- **Gmail** (`gmail.py`, `google-api-python-client`, extra `google`): 본문 텍스트는
  `text/plain` MIME 파트를 우선 사용하고(base64url, 패딩 보정), 없으면 Gmail이 항상 제공하는
  `snippet`으로 폴백한다(HTML 파싱은 하지 않음). 제목/발신자/날짜는 헤더에서 뽑아 메타데이터로
  남긴다.
- **Google Drive** (`gdrive.py`, `google` extra 공유): 일반 파일은 `get_media`, 네이티브
  Google Docs/Sheets 같은 워크스페이스 파일은 `export_media`(Docs→`text/plain`, Sheets→
  `text/csv`, 그 외 워크스페이스 타입은 `text/plain` 기본값)로 다운로드한다 — 모든 워크스페이스
  타입을 다 특별 처리하지는 않는다.
- **Notion** (`notion.py`, `notion-client`, extra `notion`): 블록 트리를 재귀적으로 순회하며
  각 블록의 `rich_text`가 있으면 뽑는 실용적 접근이다 — 모든 블록 타입을 전사하는 완전한
  변환기가 아니다. 페이지 제목은 `properties` 중 `type == "title"`인 속성에서 추출한다.

## 검증 상태와 한계

**전부 SDK 클라이언트를 모킹한 테스트로만 검증됐다** — 이 환경에는 실제 Slack/Gmail/Google
Drive/Notion 계정·토큰이 없어 라이브 API 응답으로는 검증하지 못했다. 특히:

- Gmail/Drive는 `google-api-python-client`의 `client.resource().method(**kwargs).execute()`
  체이닝 형태를 흉내 낸 가짜 객체로 테스트했다 — 실제 응답 필드명/페이지네이션 동작이 미묘하게
  다를 위험이 완전히 배제되지는 않는다.
- Notion/Slack은 각 SDK가 반환하는 dict 형태를 최대한 정확히 재현했지만 역시 실 토큰으로
  검증 전까지는 완전히 신뢰할 수 없다.

실사용 전에는 각자의 실제 토큰/계정으로 `initial_sync()`을 한 번 돌려 결과를 확인하는 걸
권장한다. `tests/collectors/test_slack.py`/`test_gmail.py`/`test_gdrive.py`/`test_notion.py`가
각 SDK의 모킹 패턴을 보여주는 참고 자료다.
