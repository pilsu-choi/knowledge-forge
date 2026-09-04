# 재적재(replay) — 이미 수집한 원본 바이트를 다시 파싱하기

`knowledge-forge reparse` · `collectors/replay.py` · `storage/replay_index.py`

## 왜 필요했나 — Google Drive 246건 중 239건이 문서가 안 됐다

개발 DB 실측(2026-09-04):

| source_type | sources | documents | chunks |
|---|---|---|---|
| google_drive | 246 | **7** | 471 |
| slack | 197 | 197 | 197 |
| gmail | 59 | 59 | 268 |

slack/gmail은 1:1인데 gdrive만 239건이 `documents` 행을 못 만들었다.

### 확정된 원인 (코드 + DB로 확인)

`sources.collected_at`을 보면 gdrive 수집은 두 번 돌았다.

- **1차 02:56–03:06 UTC — 235건 수집, 문서 0건.** 이 시점 `parsers/registry.py`는 파서를
  **URI 확장자만으로** 골랐다. GDrive의 `uri`는 `webViewLink`
  (`https://drive.google.com/file/d/…/view?usp=drivesdk`)라 확장자가 없다 → 전부
  `UnsupportedFormatError` → `pipeline.py`가 `logger.error(...); continue`로 **조용히 스킵**.
  `record_source()`는 파싱보다 **먼저** 호출되므로 `sources` 행과 디스크 원본은 남고
  `documents`만 안 생긴 상태가 됐다.
- **2차 03:19–03:26 UTC — 11건 수집, 7건 문서화.** 이때는 확장자 폴백 수정이 워킹트리에
  들어가 있었고(커밋은 12:56 KST의 `b6c9435`), 그래서 문서가 생겼다. 남은 4건은
  `Makefile` / `.env*.example`로 확장자 자체가 없어서 여전히 스킵됐다.

즉 **파서 선택 버그(`b6c9435`)와 CSV 파서 부재(`46cb5af`)는 이미 고쳐져 있었고, 남은 문제는
"고친 코드로 다시 돌릴 방법이 없다"였다.** 오늘 HEAD의 레지스트리로 239건을 시뮬레이션하면
235건이 정상적으로 파서에 매칭된다(못 매칭: `.m4a`×3, `.example`×3, `.mp4`×1, 확장자 없음×1).

### 진짜 누락: raw blob으로 되돌아갈 길이 없었다

`RawBlobStore`는 수집한 바이트를 content-addressed로 디스크에 남기고 `sources.raw_path`에
경로를 기록한다. 239건 **전부 파일이 디스크에 존재했다**(0건 유실, 0바이트 파일 없음).
그런데 그 바이트를 파이프라인에 되먹일 수단이 없어서, 파서가 고쳐져도 유일한 복구 경로가
**Drive API 재수집**(재인증 + 재다운로드 + 원격이 바뀌었으면 기준선까지 바뀜)이었다.
이게 이번에 메운 구멍이다.

## 설계

```
sources(+documents LEFT JOIN)  →  ReplayRow  →  RawBlobReplayCollector  →  run_pipeline
   storage/replay_index.py                        collectors/replay.py
```

- **`RawBlobReplayCollector`는 평범한 `Collector`다.** `initial_sync()`가 `raw_path`의
  바이트를 읽어 `RawItem`을 만들어 yield할 뿐이라, 파서/청커/추출/저장 단계는 원래 수집과
  **완전히 동일한 코드 경로**를 탄다. 재적재 전용 파이프라인을 따로 만들지 않았다.
- **`source_type`이 인스턴스 속성이다.** `pipeline._make_source()`가
  `stable_id(source_type.value, uri)`로 `source_id`를 다시 계산하므로, 재적재가 원래와 다른
  타입을 보고하면 같은 파일에 **중복 `sources` 행**이 생긴다. 그래서 replay는 자기 타입을
  갖지 않고 재생하는 행들의 타입을 그대로 쓴다(`Collector.source_type` ClassVar를 인스턴스
  속성으로 좁히는 유일한 구현체 — mypy `# type: ignore[misc]` 한 줄의 이유).
- **`get_cursor()`가 항상 비어 있다.** 재적재가 `sync_cursors`를 건드리면 다음 정기 동기화가
  이 실행이 도달한 적 없는 위치에서 재개된다. CLI도 커서를 저장하지 않는다.
- **재개(resume)는 상태 없이 된다.** 대상 선택이 `documents`와의
  `LEFT JOIN … IS NULL`이므로, 중간에 죽어도 그냥 다시 돌리면 아직 못 만든 것만 다시 고른다.
- **`content_hash`는 디스크 바이트로 재계산한다.** 파싱되는 실체는 blob이므로 다시 쓰는
  `sources.content_hash`도 그 blob을 가리켜야 한다.
- **선택 쿼리를 `postgres_store.py`가 아니라 별도 모듈에 뒀다.** 파이프라인 영속화 facade의
  일부가 아니라 CLI 한 개가 쓰는 일회성 복구 쿼리이고, 재적재가 **쓰기용으로 여는
  `PipelineStore`와 무관하게** 대상을 먼저 고를 수 있어야 해서 짧게 자체 커넥션을 연다.

### 사용법

```bash
# 무엇이 걸리는지만 본다 (파싱 안 함)
knowledge-forge reparse --source-type google_drive --dry-run

# 확장자를 골라서 재적재 (추출/임베딩 없이 Document/Chunk만)
knowledge-forge reparse --source-type google_drive \
    --extensions ".md,.html,.xlsx,.csv" --no-extract --no-embed
```

`--extract`/`--embed`는 `extract` 명령과 달리 **기본이 꺼짐**이다. 재적재의 목적은 과거
실행이 못 만든 Document/Chunk를 채우는 것이고, 복구된 백로그 전체에 LLM 추출이나 유료
임베딩을 다시 태우는 건 별개의(그리고 훨씬 비싼) 결정이라서다.

`--extensions`가 필터링에 쓰는 확장자는 레지스트리가 **디스패치에 쓰는 바로 그 확장자**다
(`parsers/registry.py::extension_for` — 이번에 공개 함수로 뽑았다). 규칙을 두 벌 두면
"고른 파일 집합"과 "실제로 파싱되는 파일 집합"이 조용히 어긋난다.

## 적재 결과 (2026-09-04)

이미지/오디오/비디오는 이번 범위에서 제외했다 — `.png` 114, `.jpg`/`.jpeg` 19,
`.m4a` 3, `.mp4` 1. 텍스트가 아니거나(오디오/비디오는 파서 자체가 없다) 전량 VLM 유료
호출이 필요해서, "이미 있는 파서로 살릴 수 있는 것부터"라는 이번 작업 범위 밖이다.

VLM 없이 되는 65건(`.md` 13 + `.html` 26 + `.xlsx` 23 + `.csv` 3)을 재적재했다.

| source_type | sources | documents (전) | documents (후) | chunks (전) | chunks (후) |
|---|---|---|---|---|---|
| **google_drive** | 246 | **7** | **70** | **471** | **1,426** |
| slack | 197 | 197 | 197 | 197 | 197 |
| gmail | 59 | 59 | 59 | 268 | 268 |

전체 코퍼스는 263 → **326 문서**. 재적재가 만든 것만 떼어 보면:

| 확장자 | mime_type | documents | chunks |
|---|---|---|---|
| `.html` | `text/plain` | 26 | 219 |
| `.xlsx` | `…spreadsheetml.sheet` | 21 | 472 |
| `.md` | `text/plain` | 13 | 261 |
| `.csv` | `text/csv` | 3 | 3 |
| **합계** | | **63** | **955** |

청크가 0개인 문서가 2건 있다.

- `si-onboarding-prototype.html` — 파싱은 됐지만 요소가 0개다. 본문이 전부 script/style인
  JS 프로토타입 페이지라 HtmlParser가 가져올 텍스트가 없다. 정상 결과다.
- `포항공장 생산 CAPA CHART(2021)_PM53 업데이트.xlsx` — 아래 파서 버그를 고친 뒤 다시
  돌려서 문서 행까지는 만들었는데, **동시 작업 중인 다른 에이전트의 검색 평가를 흔들지
  않으려고 청킹 도중에 중단**했다(`record_chunks`는 문서당 한 번에 몰아 쓰므로 청크는
  하나도 안 들어갔다). 시트가 커서 KSS 문장 분할이 수 분 단위로 걸린다.
  나중에 `reparse --all --extensions ".xlsx"`로 마저 채워야 한다 — 문서 행이 이미 있어서
  기본값 `--only-missing`으로는 안 잡힌다.

### 살리지 못한 것과 이유

| 대상 | 건수 | 이유 | 후속 |
|---|---|---|---|
| `.png`/`.jpg`/`.jpeg` | 133 | 이번 범위 밖(전량 VLM 유료 호출) | VLM 예산 결정 후 |
| `.m4a`/`.mp4` | 4 | 오디오/비디오 파서 없음 | STT 도입 시 |
| `.docx`/`.pptx`/`.pdf` | 33 | 파서는 있으나 표/그림이 VLM 경로를 탄다 | 아래 "VLM 백로그" |
| `Makefile`, `.env*.example` | 4 | 확장자가 없거나(`Makefile`) 미등록(`.example`). mime도 `application/octet-stream`이라 힌트가 없다 | 확장자 없는 텍스트 판정 규칙 |
| `삼성_E&A_…통합WBS_v260831_삼성회신.xlsx` | 1 | **수집된 바이트가 xlsx가 아니라 Google Sheets HTML 페이지다**(`<!DOCTYPE html>`, 292KB). 재파싱으로는 못 고친다 | 재수집 + 수집물 검증 |
| `TalkFile_프로젝트_인력관리_양식_26.07.19.xlsx` | 1 | zip에 `[Content_Types].xml`이 없어 openpyxl 기본 리더가 거부. `read_only=True`로는 열린다 | 아래 "후속 과제" |

## 재적재 중에 드러난 파서 버그: `xlsx.py::_build_structured`

`포항공장 생산 CAPA CHART(2021)_PM53 업데이트.xlsx`가
`TypeError: 'str' object does not support item assignment`로 **워크북 전체**를 잃었다.

다단 헤더가 들쭉날쭉할 때 생긴다. 어떤 열의 헤더 경로가 `["PM53"]`이라 `record["PM53"]`에
문자열이 들어간 뒤, 옆 열의 경로가 `["PM53", "실적"]`이면
`node.setdefault("PM53", {})`가 그 **문자열**을 돌려주고 `node["실적"] = …`에서 터진다.
`_build_structured`가 예외를 밖으로 흘려서 `XlsxParser.parse`가 통째로 실패했다 —
헤더 하나 때문에 시트 전부를 잃는 셈이다.

수정: 경로를 걸어가다 조상 자리가 스칼라면 중첩을 포기하고 `"PM53/실적"` 한 키로 평탄화한다
(`_assign_nested`). 잃는 건 그 열의 중첩 구조뿐이고 `rows`는 그대로다 — 애초에 `structured`는
청킹이 읽지 않는 부가 뷰다. **리프 충돌은 손대지 않았다**: 거기선 원래도 덮어쓰기였고 예외가
난 적이 없어서, 이번 수정이 기존 동작을 바꾸지 않게 했다.

## 남긴 후속 과제

1. **VLM 백로그 33건** — `.docx` 16 / `.pptx` 11 / `.pdf` 6. 세 파서 모두 VLM 실패를
   **이미지·표 단위로 degrade**하므로(문서 자체는 만들어진다) 자격 증명 없이도 적재는 가능하다.
   대신 PDF 표는 pdfplumber 원시 셀 텍스트로, 그림은 설명 없이 들어간다. 실측한 VLM 호출 수
   (각 파서가 실제로 보낼 영역을 세어봤다):

   | 확장자 | 파일 | VLM 호출 |
   |---|---|---|
   | `.docx` | 16 | 28 (인라인 그림) |
   | `.pptx` | 11 | 367 (슬라이드 그림) |
   | `.pdf` | 6 | 1,466 (표 + 그림 영역) |
   | 합계 | 33 | **1,861** |

   PDF 6건이 전체의 79%다. `anthropic/claude-haiku-4.5`(OpenRouter, 현재
   `settings.openrouter_vision_model` 기본값) 기준 호출당 입력 이미지 ~1.5K + 프롬프트,
   출력 수백 토큰으로 잡으면 **대략 $5–15** 규모다(자릿수 추정이지 정밀 견적이 아니다).
   비용도 비용이지만 1,861회를 동시성 제한 아래 돌리는 실행 시간이 더 큰 제약일 수 있다.
2. **`vision/factory.py`에 `claude_agent_sdk` 분기가 없다.** `KF_LLM_PROVIDER`는
   `anthropic`/`openrouter`/`claude_agent_sdk` 3값인데 vision factory는 `openrouter`만
   분기하고 나머지는 `VisionExtractor`(Anthropic SDK 직접 호출)로 떨어진다. 이 머신은
   `claude_agent_sdk`인데 `ANTHROPIC_API_KEY`가 없어서 모든 VLM 호출이 자격 증명 오류가 된다.
   `vision/`에 `claude_agent_sdk`용 구현 파일 자체가 없다 — 분기만 빠진 게 아니라 **구현이
   없다**(`extractor.py`(Anthropic), `openrouter_extractor.py` 둘뿐). factory의 docstring이
   "mirrors extraction.factory.get_extractor()"라고 주장하는 것과 어긋난다.
3. **수집물 무결성 검증이 없다.** GDrive `_download_content()`는 `get_media()`가 돌려준 걸
   그대로 믿는다. 실제로 1건이 스프레드시트 대신 Sheets 웹페이지 HTML이었고, `content_hash`가
   그걸 그대로 축복해서 정상 수집처럼 보였다. 전체 gdrive blob을 스캔하면 이 케이스는 1건뿐이지만,
   **선언된 mime/확장자와 매직 바이트가 어긋나면 수집 실패로 처리**하는 게 맞다. 이건 재파싱으로
   고칠 수 없고 재수집이 필요하다.
4. **openpyxl 기본 리더가 거부하는 워크북에 `read_only=True` 폴백.** `[Content_Types].xml`이
   없는 워크북(비-Excel 도구가 쓴 파일)이 실제로 있었다. 다만 read-only 모드는 병합 셀
   정보와 임베디드 이미지 접근을 잃으므로, "실패보다는 낫다"를 감수할지 판단이 필요해
   이번엔 넣지 않았다.
5. **확장자 없는 텍스트 파일**(`Makefile`, `.env.example` 등 4건). mime이
   `application/octet-stream`이라 mime 폴백으로도 안 풀린다. "UTF-8로 디코딩되면 TxtParser"
   같은 판정을 넣을지, 아니면 그냥 스킵할지 결정 필요.

## 동시 작업에서 배운 것: 대량 적재는 검색 평가의 기준선을 흔든다

이 재적재와 나란히 다른 에이전트가 `eval-ir`로 검색 품질을 측정하고 있었고, **코드가 하나도
안 바뀐 구간에서** v0 골든셋 성적이 ndcg@10 0.893 → 0.885 → 0.879로 내려가고
`none_correct`가 1.000 → 0.667로 떨어졌다. `eval_run.started_at`과 `documents.created_at`이
계단으로 정확히 대응한다(12:35에 52건 적재 → 0.885, 12:37에 10건 → 0.879). 한 에이전트는
이 하락을 자기 변경 탓으로 잘못 귀속했다가 정정했다.

두 가지 교훈:

- **코퍼스를 바꾸는 작업과 검색 평가는 같은 DB에서 동시에 돌리면 안 된다.** 평가 결과에는
  코드 리비전뿐 아니라 코퍼스 스냅샷(문서/청크 수)도 같이 기록돼야 한다.
- 그럼에도 이 오염이 **실제 결함을 드러냈다**: 어휘 arm에 관련도 하한이 없어서 코퍼스가
  커질수록 `구성`/`가이드`/`서류` 같은 흔한 한국어 단어가 우연히 겹치는 문서가 늘고,
  "관련 문서 없음"이 정답인 질의가 오답이 된다. 별도 작업으로 처리 중.

이 때문에 VLM 백로그 33건과 남은 xlsx 2건의 적재는 **의도적으로 보류**했다 — 코드/테스트/
문서는 끝났고, DB 쓰기만 멈춘 상태다.
