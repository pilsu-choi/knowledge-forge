# Context Reconstruction (Source ≠ Knowledge Unit)

`src/knowledge_forge/context/` — Collector와 Parser 사이에 있는 단계로, Collector가 만든
`RawItem` 스트림을 Knowledge Extraction에 적합한 semantic unit으로 재구성한다.

## 문제의식

파이프라인은 원래 "하나의 수집 단위(`RawItem`) = 하나의 독립적인 의미 단위"를 암묵적으로
가정했다. 파일에는 맞는 가정(파일 자체가 독립적 정보 단위)이지만, SaaS 소스에서는 깨진다 —
Slack 메시지 하나, Gmail 이메일 하나만 봐서는 의미가 불완전한 경우가 흔하다. 예:

```
A: 배포 언제 할까요?          B: 오늘 오후 가능할 것 같습니다.
C: DB migration 아직...       B: 아 그러면 내일 오전으로 하죠.
A: 네 내일 10시로 합시다.
```

마지막 메시지("네 내일 10시로 합시다")만 청크로 추출하면 "무엇을 10시로 합시다"인지 사라진다.
전체 대화를 하나의 context로 묶어야 의미 있는 Knowledge Object가 나온다. 자세한 배경은
[`roadmap.md`](roadmap.md)의 "Source-aware Context Reconstruction" 절 참고.

## 인터페이스

```python
class ContextReconstructor(ABC):
    def reconstruct(self, items: Iterator[RawItem]) -> Iterator[RawItem]: ...
```

`RawItem` → `RawItem`인 이유: 재구성된 결과도 다시 `RawItem`(uri/content/content_hash/
modified_at/metadata)으로 표현해서, 이후 Parser/Chunker/Extraction은 재구성이 있었는지
전혀 몰라도 된다 — 병합된 대화 하나가 마치 파일 하나처럼 그대로 기존 Parser → Chunker →
Extractor 배선을 통과한다.

`pipeline.run_pipeline(...)`은 `context_reconstructor`를 명시적으로 받지 않으면
`default_reconstructor_for(collector.source_type)`을 사용한다:

| SourceType | Reconstructor |
|---|---|
| `LOCAL_FILE` / `NOTION` / `GOOGLE_DRIVE` | `NoopReconstructor` (항등 — 이미 독립 단위) |
| `SLACK` | `SlackConversationReconstructor` |
| `GMAIL` | `GmailThreadReconstructor` |

공통 규칙: `RawItem.deleted=True`인 아이템은 재구성 대상에서 제외하고 그대로 통과한다 — 삭제는
메시지 단위 시그널이라 병합할 의미 단위가 없다.

## Gmail — Thread Reconstruction

`GmailThreadReconstructor`(`context/gmail.py`): 들어오는 스트림 전체를 버퍼링해
`metadata["thread_id"]`로 그룹핑하고, 그룹 내부는 `modified_at` 오름차순으로 정렬한 뒤 하나의
`RawItem`으로 합친다.

- `uri`: `gmail://thread/{thread_id}`
- `content`: 메시지마다 `"From: {from} ({date})\n{본문}"` 블록, 블록 사이는 빈 줄로 구분
- `metadata`: `thread_id`, `message_ids`(원본 순서), `subject`(첫 메시지 기준), `participants`

`GmailCollector`가 이미 `metadata["thread_id"]`를 채워주고 있어 재구성 로직이 단순하다.

## Slack — Conversation Reconstruction + Segmentation

`SlackConversationReconstructor`(`context/slack.py`, `gap_seconds` 기본 1800초):

1. **채널별 그룹핑** — 서로 다른 채널의 대화는 절대 섞이지 않는다.
2. **스레드는 그 자체로 하나의 에피소드** — `thread_ts`가 있고 부모(`ts == thread_ts`)와 다른
   답글들을 부모와 함께 시간순으로 묶는다. Slack API가 이미 스레드를 명시적으로 구분해 주므로
   시간 간격 세그멘테이션 대상에서 제외한다.
3. **나머지 채널 타임라인 메시지는 시간 간격으로 세그멘테이션** — 스레드에 속하지 않는 메시지를
   `ts` 오름차순으로 정렬한 뒤, 연속한 두 메시지의 간격이 `gap_seconds`를 넘으면 새 에피소드로
   나눈다("대화 흐름이 끊기는 지점").

각 에피소드는 하나의 `RawItem`으로 합쳐진다:

- `uri`: 스레드면 `slack://{channel}/thread/{thread_ts}`, 아니면
  `slack://{channel}/episode/{첫_메시지_ts}`
- `content`: 메시지마다 `"{user}: {text}"` 블록, 블록 사이는 빈 줄로 구분
- `metadata`: `channel`, (스레드인 경우) `thread_ts`, `message_ts`(목록), `participants`

## Parser 쪽 연결: `ConversationParser`

병합된 `RawItem`의 `uri`에는 파일 확장자가 없다(`slack://...`, `gmail://thread/...`) —
기존 `ParserRegistry`는 확장자로만 디스패치했으므로, URI 스킴(`urlparse(uri).scheme`)으로도
디스패치할 수 있게 확장했다(`Parser.supported_schemes`, 기본값은 빈 집합이라 기존 Parser는
영향 없음).

`ConversationParser`(`parsers/conversation.py`)가 `{"slack", "gmail"}` 스킴을 처리한다:
`content`를 (Context Reconstruction이 만든 것과 동일한) 빈 줄 구분 블록으로 나눠 블록마다
`TextBlock` 하나씩 만들고, 맨 앞에 컨텍스트 힌트용 `Heading`을 하나 추가한다(예:
`"Slack #general 대화"`, `"Gmail Thread: Deploy schedule"`).

## Chunker/Extraction은 왜 그대로인가

병합된 대화 하나가 토큰 예산 안에 들어오면 `AdaptiveChunker`가 `HierarchyLevel.DOCUMENT` 레벨
그대로 청크 하나로 유지하므로, 대화 전체가 한 번에 Knowledge Extraction에 들어간다 — 위 예시의
"Decision" 같은 문맥 의존적 Knowledge가 이렇게 만들어진다. 대화가 길어 예산을 넘으면 기존
Chapter/Section 단위 분할 로직이 그대로 적용된다. 즉 Context Reconstruction은 Chunker/Extractor
를 전혀 수정하지 않고, **Parser에 들어가기 전 입력을 올바른 단위로 재구성**하는 것만으로
문제를 해결한다.

## 스코프 밖으로 남은 것

- **첨부파일의 File Pipeline 라우팅** — Slack/Gmail 등 SaaS 커넥터가 아직 첨부파일 자체를
  감지/추출하지 않는다([`saas-connectors.md`](saas-connectors.md)). Context Reconstruction은
  텍스트 본문만 다룬다.
- **Ingest/Process 분리(큐/워커)** — `run_pipeline`은 이제 asyncio 기반으로 청크/이미지 단위
  LLM 호출을 동시 실행하지만([`architecture.md`](architecture.md) 참고), 이건 한 프로세스
  안에서의 동시성일 뿐 "요청을 받는 즉시 응답하고 처리는 별도 워커가 넘겨받는" 구조는 아니다.
  재구성 단계는 스트림을 전량 버퍼링(Gmail은 전체, Slack은 채널 단위)하므로 대량 히스토리 초기
  동기화 시 메모리 사용량이 늘어난다 — 별도 인프라 결정이 필요해 이번 범위에서 다루지 않는다.
- **SaaS 커넥터 CLI/OAuth 연동** — `knowledge-forge` CLI에는 여전히 Slack/Gmail을 실행할 명령이
  없다. 실제로 이 재구성 단계가 실행되려면 1단계(CLI/인증 연동)가 먼저 필요하다.

자세한 잔여 범위는 [`roadmap.md`](roadmap.md) 참고.

## 문서 간 링크: `sources.metadata.links` → `document_links` 테이블

재구성 단계가 `metadata["links"] = [{"type", "target_uri"}]`로 남기는 에피소드/스레드 간
관계(`SAME_CHANNEL_SEQUENCE`, `THREAD_SEQUENCE`)는 원래 OKF `links/*.yaml`
(`pipeline._resolve_links`, [`okf.md`](okf.md))에만 적재됐다. 이제 같은 항목을 Postgres
`document_links` 테이블에도 적재한다(`document_links.build_source_links`, 설계는
[`storage.md`](storage.md)의 "문서 버전 메타데이터와 document_links" 절):

- `target_uri`는 스토어에서 `sources.uri → documents.document_id`로 해석한다
  (`PostgresStore.find_document_ids_by_source_uris`). 같은 런의 in-memory uri 맵만 보는
  `_resolve_links`와 달리 **이전 런에서 적재된 대상**도 해석되고, 파싱에 실패해 Document가 없는
  대상은 조용히 건너뛴다(연결할 끝점이 없으므로).
- 방향은 `_resolve_links`와 동일: `from` = 앞선 에피소드(`target_uri`), `to` = 그 다음 것.
- **`THREAD_PARENT`**(Slack 전용): 재구성이 부모 메시지와 답글을 한 에피소드로 합치므로 보통은
  스레드 안에 부모가 이미 들어 있다. 하지만 증분 동기화에서 부모는 앞선 런의 타임라인 에피소드로,
  답글은 다음 런의 스레드 에피소드로 따로 적재될 수 있다. 이 경우 같은 채널에서 `message_ts`에
  `thread_ts`를 포함하는 *다른* 에피소드 문서를 찾아 `스레드 → 부모 에피소드` 링크를 만든다
  (`find_slack_thread_parent_document`). 개발 DB에는 이런 쌍이 0건이라 실제 생성된 건 없다.
- Gmail 스레드는 재구성이 이미 답글을 한 문서로 합치므로 스레드 내부의 reply→첫 메일 링크는
  문서 단위에서 표현할 것이 없다 — `THREAD_PARENT`는 Gmail에 만들지 않는다.
- Slack 에피소드에는 **버전 family를 부여하지 않는다**(`document_links.display_name`이 None을
  반환). 채널 타임라인은 한 문서의 개정 이력이 아니다. Gmail은 제목(`Re:`/`Fwd:` 접두 제거)으로
  family를 만든다 — "주간보고 v2 보냈습니다" 같은 메일이 같은 이름의 Drive 파일과 묶일 수 있게.
