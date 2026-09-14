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

## 재적재 버그 3건 수정 (2026-09-14, `fix/reparse-bugs`)

개발 DB 사본(`knowledge_forge_reparse_test`, 실측 후 삭제)에서 `--source-type slack/gmail/
google_drive --all --no-extract --embed --embedding-source chunk`를 돌려본 결과 확인된
버그 3건. 모두 회귀 테스트가 딸린 별도 커밋.

### 1) 슬랙 재적재가 `KeyError: 'ts'`로 즉시 죽음 — `5f64863`

**원인** (`pipeline.py:191`, `context/slack.py:81`): `run_pipeline`은
`context_reconstructor`가 안 주어지면 `default_reconstructor_for(collector.source_type)`를
쓴다. 재적재가 읽는 `sources` 행은 슬랙이든 지메일이든 **이미 재구성된 에피소드/스레드다**
(`run_pipeline`이 `_make_source`보다 먼저 `ContextReconstructor`를 돌려서 그 결과만
`sources`에 남기 때문 - 원본 메시지 낱개는 `sources` 행이 된 적이 없다). 그런데
`reparse`는 이 사실을 모르고 같은 `default_reconstructor_for(SLACK)`을 또 돌렸다.
`SlackConversationReconstructor._reconstruct_channel`의 첫 줄
`by_ts = {m.metadata["ts"]: m for m in messages}`가 병합된 에피소드 메타데이터
(`message_ts` 리스트만 있고 `ts` 없음)에서 즉시 `KeyError`를 던져 배치 전체가 죽었다.

**수정**: `Collector.already_reconstructed: ClassVar[bool] = False`
(`collectors/base.py`)를 추가하고 `RawBlobReplayCollector`가 `True`로 켠다
(`collectors/replay.py`). `run_pipeline`은 이 플래그가 서 있으면
`default_reconstructor_for` 대신 `NoopReconstructor`를 쓴다 - "재적재된 항목은 이미
리컨스트럭터의 출력물"이라는 사실을 컬렉터 쪽이 선언하는 형태라, 향후 다른 재적재류
컬렉터에도 그대로 적용된다.

**실측(전/후)**: 슬랙 소스 4건(`sources`/`documents` 4:4, 사본 DB) 대상
`--all --no-extract --no-embed`.

| | 수정 전 | 수정 후 |
|---|---|---|
| 결과 | `KeyError: 'ts'`, 배치 전체 중단, 0건 처리 | 4건 중 4건 파이프라인 통과 |
| documents | 4 (변화 없음, 크래시) | 4 (동일 document_id로 재기록) |

### 2) 지메일 재적재가 문서를 중복 생성 — `5f64863`

**원인** (`context/gmail.py:39,57`): `GmailThreadReconstructor`는 위와 같은 이유로 재적재된
스레드 항목을 다시 받지만, 메타데이터에 `thread_id`가 남아 있어 **죽지 않고** 같은
스레드로 재그룹된다. 문제는 `_merge`가 이미 병합된 본문을 `_format_message`로
`"From: unknown\n" + <이미 병합된 본문>`으로 **한 번 더** 감싼다는 것
(병합 메타데이터에는 `from`/`date` 키가 없어 `unknown`으로 떨어진다). 그 결과
`content_hash`가 바뀌고, `document_id = stable_id("document", uri, content_hash)`
(`parsers/conversation.py:35`)도 바뀐다. `uri`(=`source_id` 계산 키)는 그대로라
`sources` 행은 upsert되지만, `documents`는 `document_id`가 다른 새 행이 하나
더 생기고 **원래 행은 그대로 남는다** - `--all` 재적재 대상 59건이 118건이 된 사고.

**수정**: 1번과 같은 커밋, 같은 메커니즘(`already_reconstructed` → `NoopReconstructor`).
재구성을 건너뛰므로 재적재된 항목의 `uri`/`content`가 디스크 바이트 그대로 파서에
들어가 `document_id`가 최초 적재와 정확히 같아진다 - "문서가 legitimate하게 바뀌면
교체"가 아니라 "애초에 안 바뀐 것으로 정확히 인식"하는 쪽으로 고쳤다: 재적재의 존재
이유 자체가 "바뀐 게 없는 바이트를 새 코드로 다시 파싱하는 것"이라, 재구성을 생략하면
멱등성이 공짜로 따라온다.

**실측(전/후)**: 지메일 소스 4건(`sources`/`documents` 4:4, 사본 DB) 대상
`--all --no-extract --no-embed`.

| | 수정 전 | 수정 후 |
|---|---|---|
| documents | 4 → **8** (전부 중복) | 4 → **4** (변화 없음) |
| document_id 일치 | 새 id 4개 생성, 원본 4개 잔존 | 재적재된 4개 전부 원본과 **정확히 동일한 document_id** |

재현 중 부작용 한 가지를 여기 남긴다: 수정 전 코드로 이 재현을 돌리는 과정에서
`record_source`가 (재구성으로 바뀐) 새 `content_hash`로 원본 블롭 바이트를
**실제 `KF_DATA_DIR`에 새로 write**했다(content-addressed라 원본 파일은 안 건드렸지만
새 파일 4개가 남았다). 파일 삭제가 세션 권한으로 막혀 그대로 남아 있다 - 새 해시
경로라 아무 `sources.raw_path`도 가리키지 않는 고아 파일이라 기능상 해는 없지만,
`KF_DATA_DIR/raw/default/{99,90,50,a5}/...` 4개는 다음에 청소가 필요하다.
재현에 쓴 `knowledge_forge_reparse_test` DB 자체는 검증 후 `dropdb`로 지웠다.

### 3) 임베딩 요청이 프로바이더 한도를 넘겨 재적재 전체가 죽음 — `bdce362`

**원인** (`storage/store.py`의 `record_chunk_embeddings`/`record_embeddings`,
수정 전 `pipeline.py`의 호출부): 두 메서드 모두 문서 한 건의 청크/KnowledgeObject
**전부**를 `embedder.embed()` 한 번에 몰아 보냈다. 청크가 많은 문서(대형 PDF 등)를
reparse하면 OpenRouter가 `HTTP 400 ... maximum request size is 300000 tokens per
request`를 돌려줬는데, `pipeline.py`가 이 호출을 try/except로 감싸지 않아 예외가
`run_pipeline` 제너레이터 밖으로 그대로 새어나가 **그 문서 이후로는 아무것도 처리하지
못하고 재적재 전체가 죽었다**(google_drive reparse 사고).

**수정**:
- `embedding/base.py`: `EmbeddingProvider.embed()`를 concrete로 바꾸고, 실제 API
  호출은 새 추상 메서드 `_embed_batch()`로 옮겼다. `embed()`는 기존 토크나이저
  (`chunking/tokenizer.py`, cl100k_base)로 토큰 수를 세어 `_batch_by_budget()`으로
  텍스트를 요청 단위(기본 250,000토큰 / 2,048개 항목, 항목당 권장 상한
  8,191토큰)로 쪼갠 뒤 각 배치를 `_embed_batch()`에 넘기고 결과를 순서대로 이어
  붙인다 - 한 곳에서만 구현해서 `record_embeddings`/`record_chunk_embeddings` 양쪽이
  따로 배치 로직을 만들 필요가 없다(요청한 대로 "provider base"에 구현).
- `pipeline.py`: 두 임베딩 호출을 try/except로 감싸 로그만 남기고 다음 문서로
  진행하도록 했다 - 파서 실패를 이미 이렇게 다루던 컨벤션과 동일. 실패한 문서의
  Document/Chunk는 임베딩 이전에 이미 저장됐으므로 그대로 남는다(임베딩만 없는 채로).

**실측**: 300k 토큰 과다 요청 자체는 사본 DB에서 값싸게 재현하기 어렵다(원인이 된
"나에게맞춘간편건강보험약관.pdf"급 문서는 7,643청크 - 실비용/시간이 커서 이번 검증
범위에서 제외, 단위 테스트로 대체). 대신:
- **단위 테스트**로 분할 로직 자체를 검증했다(`tests/embedding/test_embedding_batching.py`):
  예산 이내는 요청 1번, 300,000토큰짜리 입력은 여러 요청으로 분할, 순서 보존, 빈
  입력, 예산을 혼자 넘는 단일 항목 처리.
- **실 API 스모크 테스트**(OpenRouter, `text-embedding-3-small`, 사본 DB): google_drive
  중간 크기 문서 2건(xlsx, 71청크/138청크)을 `--all --no-extract --embed
  --embedding-source chunk`로 재적재 - 209개 청크 전부 청크당 임베딩 1개씩 정확히
  기록됨(배치 분할까지는 안 갈 크기라 회귀 없음만 확인, 분할 자체는 위 단위 테스트가
  본다).
- **컨테인먼트**는 `tests/test_pipeline_embedding_failure_containment.py`로 검증:
  2문서 중 1문서만 임베딩이 실패해도 두 문서 다 파싱·저장까지는 끝까지 처리됨(실패한
  쪽만 임베딩이 비어 있음).

### ~~남은 구멍: 재적재가 문서의 예전 청크를 지우지 않음~~ → 아래 "재적재 시 문서의 청크 집합 교체"에서 해결

이번 3건과 직접 얽힌 코드 경로가 아니라서(콘텐츠는 안 바뀌므로 청크 집합도 안 바뀐다)
고치지 않고 여기 보고만 한다. `storage/postgres_store.py::save_chunks`는
`chunk_id` 기준 upsert뿐이고, 한 문서의 예전 청크 중 새 청크 집합에 없는 것을 지우는
코드가 없다. 파서/청커 로직이 바뀌어 같은 문서가 **더 적은** 청크로 재적재되면
(`document_id`는 그대로인 정상 케이스 - 콘텐츠가 안 바뀌었으니), 사라진 청크와 그
청크-grain 임베딩이 고아로 남는다. 고치려면 `record_chunks`가 "이 document_id의
새 청크 집합"을 알아야 하므로 트랜잭션 안에서 `DELETE FROM chunks WHERE document_id
= ? AND chunk_id NOT IN (...)` 형태(그리고 `knowledge_object_embeddings`의 대응
행도 함께)가 필요하다 - 다음 재적재 작업의 범위로 남긴다.

## E3 처리량 (`perf/etl-concurrency`)

### 재적재 시 문서의 청크 집합 교체

`PostgresStore.save_chunks(chunks, document_id=...)`가 이제 그 문서의 **청크 집합 전체를 교체**한다:
기존 청크 중 새 집합에 없는 것과 그 청크-grain 임베딩 행을 upsert와 **같은 트랜잭션**에서 지운다.
`pipeline.py`는 `PipelineStore.record_chunks(document_id, chunks)`로 항상 문서 id를 넘기므로 빈
청크 집합(예: VLM 실패로 이미지 설명이 사라진 문서)도 예전 청크를 전부 지운다. `document_id` 없이
부르면 예전처럼 upsert만 한다(테스트 픽스처처럼 여러 문서 청크를 한 번에 넣는 호출용).

- **순서 제약**: `knowledge_object_embeddings.chunk_id → chunks` FK가 있어 임베딩 행을 먼저 지운다.
  `chunks.parent_chunk_id`는 FK가 없고(현재 청커는 항상 None) 다른 FK도 chunks를 가리키지 않는다.
- **JSON 참조**: `knowledge_objects.evidence`/`relations.evidence`의 chunk_id는 FK가 아닌 JSON이라
  지워진 청크를 가리킨 채 남을 수 있다. 이걸 역참조하는 읽기 경로는 없다 - `read_document`는
  `chunks` 테이블을 직접 읽고, 검색은 KO 행 자체(요약/본문/임베딩)를 돌려줄 뿐 evidence로 청크를
  조회하지 않는다. DB 테스트(`test_save_chunks_with_document_id_replaces_the_documents_chunk_set`)가
  이 상태에서도 KO를 그대로 읽을 수 있는지 확인한다.
- **커넥션**: 트랜잭션은 풀에서 빌린 전용 커넥션에서 연다(`_read_conn` → `_pooled_conn`). 공유
  autocommit 커넥션에서 열면 문서 간 동시 처리 중인 다른 코루틴의 문장이 그 트랜잭션에 섞인다.

### 실측 (복제 DB `knowledge_forge_e3`, 실측 후 삭제)

개발 DB 스냅숏(`/tmp/kf_etl.dump`, 358문서/2,856청크, 청크 병합 이전 청크)을 매 실행 전에 새로
복원하고, 문서가 있는 google_drive 소스 **26건 고정**(pdf 5, pptx 7, xlsx 9, docx 5 - 대형 pptx
3개, 약관 OCR pdf, 4MB xlsx 2개 포함)을 `reparse --source-type google_drive --all --no-extract
--no-embed --uri ...`로 돌렸다. `KF_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=`라 VLM은 자격 증명
오류로 **네트워크 호출 없이 즉시 실패**하고(실행당 773건) 표는 pdfplumber 추출로, 그림은 설명 없이
강등된다 - 최초 적재 때와 같은 조건이다.

| 코드 | 벽시계 |
|---|---|
| main `90e8515` | 72.6s, 67.5s (구간 계측 켠 실행 67.8s, 67.8s) |
| 청크 집합 교체만 `4262e06` | 70.3s |
| 이 브랜치, `KF_MAX_CONCURRENT_DOCUMENTS=4`(기본) | 53.5s, 54.0s (계측 53.7s) |
| 이 브랜치, `KF_MAX_CONCURRENT_DOCUMENTS=1` | 54.4s |

구간별 누적 시간(파서 `parse`, 청커 `chunk`, `PipelineStore` 쓰기를 감싸 합산, 초):

| 구간 | main | 이 브랜치 동시성 1 | 이 브랜치 동시성 4 |
|---|---|---|---|
| PDF 파싱 | 38.3 | 37.6 | 63.1 |
| XLSX 파싱 | 18.6 | **6.2** | 17.4 |
| PPTX / DOCX 파싱 | 0.6 / 0.15 | 0.6 / 0.15 | 3.6 / 4.2 |
| 청킹 | 5.1 | 5.3 | 7.9 |
| DB 쓰기 합(source/document/link/chunks) | 0.66 | 0.78 | 13.8 |

- **벽시계 이득 약 14초(67.8 → 54초)는 거의 전부 xlsx 수식 이중 로드 제거다**(18.6 → 6.2초).
  전체 `data_only=False` 재로드는 셀 객체와 스타일을 전부 만든다. read-only 스트리밍 패스는 그걸
  만들지 않는다. 단독 벤치(xlsx 23건, 로드만)의 4.48 → 1.10초보다 파이프라인 안의 이득이 큰 건,
  워크북 두 개를 들고 있던 메모리/GC 비용까지 함께 빠졌기 때문으로 보인다(분리 측정은 안 했다).
- **이 측정 조건에서는 문서 간 동시성의 벽시계 이득이 없다**(동시성 4: 54.0초, 1: 54.4초).
  VLM이 네트워크 없이 즉시 실패해 I/O 대기가 없고, 남는 일(pdfminer·openpyxl·kss pecab)은 순수
  파이썬이라 GIL 하나를 나눠 쓴다. 동시성 4의 구간 합이 벽시계를 넘는 건 코루틴이 GIL과 이벤트
  루프 차례를 기다린 시간이 각 구간에 함께 잡혀서다. DB 쓰기 합이 13.8초로 뛴 것도 같은 이유다
  (쓰기 자체는 동시성 1에서 0.78초). 동시성의 이득은 VLM/임베딩/추출 호출이 실제로 네트워크를 탈 때
  나온다 - 아래 임베딩 백필이 그 경우다. 청킹과 파싱을 다른 프로세스로 옮기지 않는 한 CPU만 쓰는
  재적재는 더 빨라지지 않는다(후속 과제 후보).
- main 쪽 구간 합(DB 쓰기 0.66초 / 벽시계 67.8초, 1%)을 보고 쓰기 풀을 두지 않았다
  ([`architecture.md`](architecture.md#문서-간-동시성)).

**결과 행 동일성** (`documents`는 id·elements md5·metadata md5·title, `chunks`는 id·text md5·
token_count·level·sequence·element_ids·page 범위를 비교):

- `documents` 26행: main 실행과 이 브랜치 실행이 **완전히 같다**(동시성 1/4 모두).
- `chunks`: 이 브랜치 결과 1,584행은 main 결과 2,806행의 **부분집합**이다. 차이 1,222행은 전부
  복원 직후에도 있던 행(병합 이전 청크)이고, 새 결과와 chunk_id가 겹치는 행은 없다 - 즉 차이는
  의도한 stale 청크 삭제뿐이다. 반복 실행끼리(main 두 번, 이 브랜치 두 번)도 행이 같다.
- 전체 합계: chunks 2,856 → main 3,876(새 청크가 예전 청크 **옆에 쌓임**) / 이 브랜치 2,654,
  청크 임베딩 2,856 → main 2,856(stale 1,222행 잔존) / 이 브랜치 1,634. documents 358,
  knowledge_objects 5,107, document_links 204, sources 502는 모두 그대로.
- 파서 출력 자체도 따로 확인했다: 고정 26건 + 개발 DB xlsx 전체 23건 + 픽스처 7건(총 47건)을 두
  코드로 파싱한 Document JSON 해시가 전부 같다(파싱 불가 xlsx 2건은 양쪽 같은 오류). 수식이 있는
  xlsx 7건, 수식 셀 6,606개.

**`embed --embedding-source chunk` 백필**: 같은 스냅숏에서 고정 26건의 청크 임베딩 1,786행을
지우고 채웠다(OpenRouter `openai/text-embedding-3-small`, 배치 64).

| 코드 | 벽시계 | 결과 |
|---|---|---|
| main (배치 하나씩 조회→임베딩→기록) | 48.2s | 1,786행, 누락 0 |
| 이 브랜치 (조회 1회에 배치 5개분, 겹치지 않게 동시 임베딩) | **15.9s** (3.0배) | 1,786행, 누락 0, chunk_id·embedded_text_hash가 main과 동일 |

동시 배치 수는 새 설정 없이 `KF_MAX_CONCURRENT_LLM_REQUESTS`(기본 5) - 프로바이더 세마포어와 같은
값이다. 다음 조회는 그 조회의 쓰기가 모두 끝난 뒤에만 하므로 같은 행이 두 배치에 들어가지 않는다.

