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

**한 건만 다시 파싱할 때는 `--uri`를 써라.**

```bash
knowledge-forge reparse --source-type google_drive --all \
    --uri "https://docs.google.com/spreadsheets/d/…/edit" --no-extract --no-embed
```

이미 문서가 있는 파일 하나를 고치려고 `--all --extensions ".xlsx"`를 쓰면 멀쩡한 형제
20건까지 같이 재파싱된다. id가 결정적이라 행이 중복되지는 않지만
(`document_id=stable_id("document", uri, content_hash)`,
`chunk_id=stable_id(document_id, index, level)`), **형제들의 `elements`/`updated_at`이
전부 다시 쓰이고**, 재파싱이 이전보다 청크를 **적게** 내면 남는 청크가 그대로 방치된다
(청크 쓰기는 upsert뿐이고 orphan을 지우는 코드가 없다). 20건을 흔들어 1건을 고칠 이유는 없다.

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
  문서 행이 이미 있어서 기본값 `--only-missing`으로는 안 잡히므로,
  `reparse --all --uri "<그 소스의 uri>"`로 그 한 건만 채운다(위 `--uri` 설명 참고).
  `--all --extensions ".xlsx"`는 멀쩡한 20건까지 끌고 들어가므로 쓰지 않는다.

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

1. **VLM 백로그 33건 — VLM 없이 적재하기로 승인됨(실행 대기).** `.docx` 16 / `.pptx` 11 /
   `.pdf` 6. 세 파서 모두 VLM 실패를 **이미지·표 단위로 degrade**한다. 자격 증명 없이 33건을
   실제로 파싱해봤고(**DB 쓰기 없이**) 예외 0건, 총 **33문서 / 8,608청크 / 파싱 119초**다.

   | 확장자 | 파일 | 청크 | 본문 문자 | 파싱 시간 |
   |---|---|---|---|---|
   | `.docx` | 16 | 92 | 23,179 | 1s |
   | `.pptx` | 11 | 786 | 25,938 | 4s |
   | `.pdf` | 6 | 7,730 | 876,910 | 114s |

   VLM을 태웠다면 필요했을 호출은 **1,861회**(docx 28 / pptx 367 / pdf 1,466)이고,
   `anthropic/claude-haiku-4.5` 기준 대략 $5–15 규모다(자릿수 추정).

   적재 전에 알아야 할 것:

   - **`나에게맞춘간편건강보험약관.pdf` 한 건이 7,643청크(전체의 89%)다.** 997페이지 보험약관이라
     이것만으로 코퍼스 청크가 1,426 → 약 10,000으로 7배가 된다. 골든셋과 무관한 한국어 약관
     보일러플레이트라 어휘 arm의 term 변별력 문제를 정확히 악화시킬 후보다 —
     **그래서 빼지 않고 따로 적재한다**(아래 "2단계 분할" 참고).
   - **본문이 0자인 PDF가 3건**이다 — `ASME B36.10-2022`(34p, 이미지 41개, 청크 0개),
     `WhaTap_AIOps_Presentation`(21p, 표 23개뿐, 이미지 426개), `Design.pdf`(청크 1개).
     스캔/이미지 전용 문서라 VLM 없이는 사실상 빈 문서가 된다. VLM을 태울 가치가 있다면
     정확히 이 3건이다.
   - `.pptx` 786청크 중 777청크가 `structure_size.pptx`(283)와
     `(공유용) DTS Equipment Cata별 Spacing 정의`(494) 두 건에서 나온다.

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

## 2단계 분할 적재 계획 — 약관 PDF를 왜 빼지 않고 나중에 따로 넣나

VLM 백로그 33건을 **A(32건) → B(약관 1건) → C(CAPA CHART xlsx 1건)** 로 나눠 적재한다.
33건 전부 결국 들어간다. 나누는 건 회피가 아니라 **영향을 격리해서 측정**하기 위해서다.

| 단계 | 대상 | 선택 방법 | 예상 |
|---|---|---|---|
| A | docx 16 + pptx 11 + pdf 5 | `--extensions ".docx,.pptx,.pdf" --exclude-uri "<약관>"` | 32문서 / 965청크 |
| B | `나에게맞춘간편건강보험약관.pdf` | `--uri "<약관>"` | 1문서 / 7,643청크 |
| C | `포항공장 … CAPA CHART … .xlsx` | `--all --uri "<CAPA>"` | 1문서 / 청킹 7분+ |

`--dry-run`으로 A=32건(pdf 5), B=1건 확인했다. C는 A/B의 측정 사이에 끼우지 않는다 —
청킹이 오래 걸려서 어느 측정 구간에 걸쳤는지 불분명해진다.

### A 적재 완료 (2026-09-04)

**32건 전부 통과, 파싱 실패 0건.** VLM 호출은 869회 시도 전부 자격 증명 오류로
이미지 단위 degrade — 유료 호출 0건.

| source_type | sources | docs 전 | docs 후 | chunks 전 | chunks 후 |
|---|---|---|---|---|---|
| **google_drive** | 246 | 70 | **102** | 1,426 | **2,391** |
| slack | 197 | 197 | 197 | 197 | 197 |
| gmail | 59 | 59 | 59 | 268 | 268 |
| **전체** | 502 | **326** | **358** | **1,891** | **2,856** |

A가 만든 것: **32문서 / 965청크** (사전 파싱 예측과 정확히 일치).

| 확장자 | documents | chunks |
|---|---|---|
| `.docx` | 16 | 92 |
| `.pptx` | 11 | 786 |
| `.pdf` | 5 | 87 |

`documents.title` **non-NFC 0건**(전체 358건 기준으로도 0건). A가 적재한 32건 중 21건은
`sources.metadata.name`이 여전히 NFD인데, `derive_document_title`이 적재 시점에 NFC로
정규화하므로 어휘 검색이 맞는 쪽은 문제없다.

**청크 0개 문서(gdrive 전체 3건)**

| 문서 | elements | 원인 |
|---|---|---|
| `si-onboarding-prototype.html` | 0 | 본문이 전부 script/style인 JS 페이지 (기존) |
| `포항공장 … CAPA CHART … .xlsx` | 13 | C단계 대기 — 청킹 중 의도적으로 중단됨 (기존) |
| `ASME B36.10-2022 …(OCR).pdf` | 41 | **A 신규.** 34페이지 스캔 PDF, 41개 전부 ImageElement, 추출 가능한 텍스트 0자 |

**본문 0자지만 청크는 있는 문서 2건** — 표 셀만 청크가 된 경우다. VLM 없이는 내용이
사실상 비어 있다.

| 문서 | chunks | 본문 문자 | 이미지 | 표 |
|---|---|---|---|---|
| `WhaTap_AIOps_Presentation.shinhanlife.pdf` | 23 | 0 | 426 | 23 |
| `Design.pdf` | 1 | 0 | 7 | 21 |

**청크 쏠림**: `.pptx` 786청크 중 777청크가 두 파일에서 나온다 —
`(공유용) DTS Equipment Cata별 Spacing 정의`(494), `structure_size.pptx`(283).
`.docx` 16건 중 13건은 거의 동일한 주간보고(각 ~1.4K자, 1청크)다.

### 왜 약관을 빼면 안 되나

빼고 싶은 유혹이 정확히 이번 사고의 원인이다. 어휘 arm은 **263문서짜리 얌전한 코퍼스에서만**
측정하고 상시 경로로 승격됐고, 코퍼스가 326문서로 늘자마자 `none_correct`가 1.000 → 0.667로
무너졌다(`구성`/`가이드`/`서류` 같은 흔한 한국어 단어가 우연히 겹치면 "관련 문서 없음"이
정답인 질의가 오답이 된다). 여기서 약관을 빼면 그 수정도 다시 "얌전한 코퍼스에서만 검증된 것"이
되어 **같은 실수를 한 번 더 반복한다.**

흔한 단어가 수백 번 반복되는 7,643청크짜리 문서는 회피 대상이 아니라 **term 변별력 수정이
진짜로 통하는지 가리는 스트레스 테스트**다. 그래서 B는 A 측정이 끝난 뒤에 들어가고, 들어간
뒤에 다시 잰다.

### `--exclude-uri`를 만든 이유

A의 32건을 `--uri`로 하나씩 나열할 수도 있었다. 같은 선택을 얻으면서 **오타 낼 기회만 32번**
늘어날 뿐이다. `--extensions`로 포맷 전체를 고르고 따로 뺄 한 건만 `--exclude-uri`로 지정하는
쪽이 훨씬 단순하고, "무엇을 뺐는지"가 명령줄에 한 줄로 남는다.

### 코퍼스를 키우면 df 추정이 더 나빠진다 (다른 작업의 발견, 여기 남겨둔다)

이 재적재가 드러낸 `none_correct` 1.000 → 0.667은 `fix/lexical-term-idf`(`ab1be6f`)에서
고쳐졌다 — `_coverage_threshold`가 매칭된 term의 *개수*만 세고 변별력을 안 봐서,
`구성`(df 26)/`가이드`(df 25) 같은 흔한 단어 3개가 한 문서에 우연히 겹치면 통과했다.
수정 후 `none_correct` 1.000 회복.

그 과정에서 나온, **재적재가 계속되면 악화되는** 문제 하나: term별 `lexical_search`의
히트 수를 df로 쓰면 `LIMIT 20` 절단 때문에 **흔한 term일수록 df가 과소평가된다**
(`인터뷰` 3 vs 실제 37, `AI` 4 vs 162). v0 질의 26개 중 7개에서 "가장 희귀한 term"의
신원이 뒤집혔다. 별도 count 질의로 고쳤지만, **코퍼스가 커질수록 이 편차는 커진다** —
앞으로 이미지 133건이나 다른 소스를 더 적재할 때 df 기반 로직을 다시 확인해야 한다.

### VLM 결정

전량 1,861회($5–15)는 쓰지 않는다. VLM이 실제로 값어치를 하는 건 **본문 0자인 스캔 3건**
(`ASME B36.10-2022`, `WhaTap_AIOps_Presentation`, `Design.pdf`)뿐이고 약 500회 $2 내외다.
A/B/C가 다 끝나고 측정이 안정된 뒤에 별도로 판단한다.

## `fix/etl-quick`: 변경 없는 소스를 건너뛴다

위 "후속 과제" 2번(vision factory 분기)과 3번(수집물 무결성)에 이어, `extract`/`/ingest`가
매번 소스 전체를 다시 파싱·재추출·재임베딩하던 낭비를 고쳤다(`pipeline.py::run_pipeline`).

`cli.py extract`(`_build_collector` → `LocalFileCollector(root=path)`)와 `ingest.py
run_local_ingest`는 로컬 파일 소스에 커서가 없어 매번 `initial_sync()`로 디렉터리 전체를
훑는다 - 그 자체는 의도된 설계다(로컬 트리는 원격 API처럼 변경분만 물어볼 방법이 없다).
문제는 그 뒤였다: 훑어서 얻은 항목마다 내용이 바뀌었는지 전혀 확인하지 않고 매번 파싱 →
청킹 → LLM 추출 → 임베딩까지 그대로 다시 돌렸다.

이제 `store`가 주어지고 `force=False`(기본값)이면, `run_pipeline`은 `record_source()`를
부르기 **전에** `store.get_source_content_hash(source_id)`로 한 번만 묻는다 - 이 메서드는
`sources.content_hash`를 그냥 읽는 게 아니라 **그 소스에 대응하는 `documents` 행이 이미
있을 때만** 값을 돌려준다(`PostgresStore.get_source_content_hash`, `sources`/`documents`
조인 한 쿼리). 돌아온 값이 이번 `content_hash`와 같으면 그 항목은 파싱도, 저장도, 추출도,
임베딩도 하지 않고 통째로 건너뛴다 - `ExtractionOutcome`도 yield하지 않는다(모든 호출부가
outcome/object 개수만 세므로 건너뛴 만큼 개수가 줄 뿐 어긋나지 않는다). 소스가 아예 처음
보이는 경우, 또는 소스 행은 있지만 과거 실행이 파싱에 실패해 `documents` 행을 못 만든 경우
(위 "왜 필요했나" 절의 gdrive 1차 수집처럼)는 둘 다 `None`이 돌아와 건너뛰지 않는다 - 이
구분이 이 메서드가 `content_hash`만이 아니라 문서 존재까지 같은 쿼리에서 보는 이유다.

`force=True`는 이 검사를 완전히 건너뛴다. `reparse`(`RawBlobReplayCollector`)는 이 플래그를
항상 켠 채로 `run_pipeline`을 부른다 - 재적재의 존재 이유 자체가 "바뀐 게 없는 바이트를 고쳐진
파서로 다시 파싱하는 것"이라, 여기서 unchanged 검사가 걸리면 재적재가 통째로 무력화된다.
`extract`/`backfill`은 기본값(`force=False`)을 그대로 쓴다.

검사 순서가 중요하다: `record_source()`는 `ON CONFLICT DO UPDATE`라 건너뛰기 판단보다
먼저 부르면 스킵 대상인데도 `sources.collected_at` 등을 다시 써버린다. 그래서 검사를
`record_source()` **앞**으로 옮겼다 - 건너뛴 항목은 정말로 아무것도 쓰지 않는다.

## gdrive 다운로드 무결성 검사

위 "살리지 못한 것과 이유" 표의 "수집된 바이트가 xlsx가 아니라 Google Sheets HTML 페이지"
사고가 재발하지 않도록, `collectors/saas/gdrive.py::_download_content`가 `get_media()`로
받은 원본 바이트를 파일 이름의 확장자가 요구하는 매직 바이트와 대조한다
(`_check_download_integrity`) - zip 기반 OOXML(`.xlsx`/`.docx`/`.pptx`/`.hwpx`)은
`PK\x03\x04`, `.pdf`는 `%PDF`로 시작해야 한다. 검사는 **확장자 키**다(Drive의 `mimeType`
필드가 아니라) - hwpx처럼 Drive가 안정적으로 인식하는 mime이 없는 포맷도 있고, 어차피
`parsers/registry.py`도 확장자로 파서를 고르므로 "이 파일이 뭐라고 주장하는가"의 기준을
그쪽과 맞췄다. Google Workspace 네이티브 파일의 `export_media()` 결과(`text/plain`/
`text/csv`)는 원본 이름의 확장자와 무관하게 의도적으로 다른 포맷이라 이 검사를 타지
않는다 - `get_media()` 분기에서만 건다.

불일치가 나면 `DownloadIntegrityError`를 던지고, `_raw_item_for_file`이 그걸 잡아 로그에
`uri` + 선언된 확장자 + 감지된 헤드(첫 32바이트)를 `logger.error`로 남긴 뒤 `None`을
돌려준다 - `initial_sync`/`incremental_sync` 양쪽 다 폴더를 건너뛰던 것과 같은 자리에서
`None`이면 `yield`하지 않는다. 그 바이트는 `RawItem`으로 만들어지지 않으므로
`content_hash`가 잘못된 콘텐츠를 정상 수집처럼 축복하는 일(위 "확정된 원인" 절)이 수집
시점에서 막힌다. 이건 재적재로 고칠 수 있는 문제가 아니다(재수집이 필요하다는 점은
그대로다) - 여기서 막는 건 **다음번에 같은 사고가 또 조용히 통과하는 것**이다.
