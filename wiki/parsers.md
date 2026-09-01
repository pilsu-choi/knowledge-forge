# Parser 지원 현황

모든 Parser는 `Parser` ABC(`parse(item, source_ref) -> Document`)를 구현하며,
`ParserRegistry`가 파일 확장자로 알맞은 Parser를 찾아 디스패치한다.

## 포맷별 현황

| 포맷 | 확장자 | 상태 | 텍스트 추출 | 표/이미지 추출 | 비고 |
|---|---|---|---|---|---|
| PDF | `.pdf` | ✅ 실제 동작 | 규칙 기반(`pdfplumber`) | **VLM 하이브리드** | 아래 "PDF 라이브러리 선택", [`vision-extraction.md`](vision-extraction.md) 참고 |
| DOCX | `.docx` | ✅ 실제 동작 | 규칙 기반(`python-docx`) | 표는 규칙 기반, **이미지는 VLM** | 스타일명(`Heading N`) 기반 헤딩 감지, `inline_shapes`의 Picture만 대상 |
| Markdown | `.md` | ✅ 실제 동작 | 규칙 기반(`markdown-it-py`) | 규칙 기반 | GFM 테이블 포함 |
| TXT | `.txt` | ✅ 실제 동작 | 규칙 기반(표준 라이브러리) | - | 빈 줄 기준 단락 분리, 헤딩 감지 없음 |
| XLSX | `.xlsx` | ✅ 실제 동작 | - | 표는 규칙 기반, **앵커된 이미지는 VLM** | 시트별 `TableElement`(셀 값이 이미 텍스트라 VLM 불필요), `worksheet._images`의 앵커 이미지는 `describe_figure()` |
| PPTX | `.pptx` | ✅ 실제 동작 | 규칙 기반(`python-pptx`) | 표·차트는 규칙 기반, **그림(Picture)은 VLM** | 네이티브 차트는 결정론적 데이터 추출, 아래 참고 |
| Image | `.png`/`.jpg`/`.jpeg` | ✅ 실제 동작 | - | **VLM 전용** | OCR(`pytesseract`) 대체 — 시스템 `tesseract-ocr` 의존성 완전히 제거됨 |
| HWPX | `.hwpx` | ✅ 실제 동작 | 규칙 기반(`python-hwpx`) | 표는 규칙 기반, **이미지는 VLM** | `python-hwpx` 라이브러리 기반, 아래 "HWPX" 참고 |

**"VLM 하이브리드"/"VLM 전용" 표시된 항목은 표/이미지/차트/다이어그램을 결정론적 파서가 아니라
항상 Claude vision 호출로 추출한다** — 실패 시에만 VLM을 쓰는 폴백 정책이 아니라, 사용자가
명시적으로 선택한 "항상 VLM" 정책이다. 설계 근거, 비용 트레이드오프, 실패 시 각 요소별 성능
저하(degradation) 동작은 [`vision-extraction.md`](vision-extraction.md)에 자세히 다룬다.

### Image 파서의 OCR → VLM 전환

Phase 1 스텁은 `Pillow` + `pytesseract`(OS `tesseract-ocr` 바이너리 필요, 한국어는
`tesseract-ocr-kor` 언어팩까지 필요)로 계획되어 있었다. Phase 2에서는 이를 전부 VLM
(`VisionExtractor.transcribe_image`)으로 교체했다 — OCR보다 나은 점 두 가지: (1) 텍스트가
거의 없는 차트/다이어그램/사진도 의미 있게 설명할 수 있고, (2) OS 바이너리 설치가 전혀
필요 없어져 배포가 단순해졌다(`pip`/`uv` 설치만으로 완결, 네트워크로 Claude API에 접근 가능하면
끝).

### PPTX 네이티브 차트: 래스터화 대신 결정론적 데이터 추출

PPTX 안의 표는 `python-pptx`로 셀 텍스트를 정확히 읽을 수 있어 규칙 기반을 유지한다(VLM은
이미 정확히 아는 값을 추측하게만 만든다). 그림(Picture) 도형은 `shape.image.blob`으로 원본
이미지 바이트를 바로 얻을 수 있어 VLM로 설명을 붙인다.

파워포인트 **네이티브 차트 객체**(그림이 아니라 차트 XML로 존재하는 도형)는 이 환경에
LibreOffice headless 같은 슬라이드 래스터화 백엔드가 없어 이미지로 만들 수 없다 — 하지만
`python-pptx`의 차트 데이터 API(`shape.chart.plots[0].categories`, `series.values`)로 카테고리·
시리즈·값을 **결정론적으로** 바로 읽을 수 있고, 이는 애초에 VLM으로 렌더링된 이미지를 "읽어서
추측"하는 것보다 더 정확하다(차트 뒤의 실제 숫자 데이터이므로). 그래서 차트는 VLM이 아니라
`TableElement`(헤더 행 `["", *categories]`, 시리즈별 데이터 행, `extraction_method=
"rule_based"`)로 렌더링한다 — `parsers/pptx.py`의 `_parse_chart()`, 자세한 설계 배경은
[`vision-extraction.md`](vision-extraction.md) 참고.

## PDF 라이브러리 선택: `pdfplumber` vs `PyMuPDF`

문서 구조(특히 헤딩)를 가장 잘 추출하는 라이브러리는 `PyMuPDF`/`pymupdf4llm`이지만, 이
라이브러리는 **AGPL-3.0** 라이선스다. 엔터프라이즈 제품에 AGPL 코드를 포함시키면 카피레프트
의무(소스 공개 요구 등)가 발생할 수 있어, Artifex의 상용 라이선스를 구매하지 않는 한 실제
리스크가 된다. 따라서 이번 구현에서는 MIT 라이선스인 `pdfplumber`(`pdfminer.six` 기반)를
기본 선택지로 사용한다. `pymupdf4llm`은 법무/구매 검토를 거쳐 상용 라이선스를 확보하는 경우
재검토 대상으로 로드맵에 남겨둔다.

`pdfplumber` 기반 헤딩 감지는 페이지 내 문자(char)의 폰트 크기/굵기를 비교하는 휴리스틱이다
— 의미적으로 헤딩을 이해하는 것이 아니라 타이포그래피 패턴을 근사하는 것이므로, 타이포그래피
컨벤션이 일관되지 않은 PDF에서는 오작동할 수 있다. 이는 알려진 한계로 문서화하며, MVP 범위에서
"고쳐야 할 버그"로 취급하지 않는다.

## HWPX: `python-hwpx` 재발견

초기 조사에서는 "리눅스에서 쓸 수 있는 성숙한 순수 Python HWPX 라이브러리가 없다"고 결론
내렸었다(`pyhwpx`는 Windows에서 한글과컴퓨터 "한글" 프로그램을 COM 자동화로 제어하는 방식이라
리눅스 서버에서 쓸 수 없고, `hwp5`/`pyhwp`는 구버전 바이너리 `.hwp`만 대상으로 하며 XML 기반
신버전 `.hwpx`는 지원하지 않았기 때문). **이 결론은 틀렸다** — 후속 조사에서 PyPI의
`python-hwpx`(활발히 유지보수되며, 자체 테스트 스위트가 실제 한컴 생성 파일 476개 중 476개를
성공적으로 열었다고 밝히는 라이브러리)를 발견해 실제로 설치·API 검증까지 마쳤고, 이를 기반으로
`HwpxParser`(`src/knowledge_forge/parsers/hwpx.py`)를 구현했다.

- 문단: `doc.paragraphs` 순회, `doc.styles.get(paragraph.style_id_ref)`의 `eng_name`이
  `"Outline N"`이거나 `name`이 `"개요 N"`이면 헤딩으로 판정 — **DOCX의 `"Heading N"` 스타일명
  컨벤션과 정확히 같은 방식**이다.
- 표: `doc.tables`의 `cell(r, c).text`로 셀 텍스트를 정확히 읽는다(규칙 기반, 표 안 문단은
  `doc.paragraphs`에 중복 포함되지 않음을 확인함).
- 이미지: `doc.media.images` + `doc.package.read(item.href)`로 원본 바이트를 얻어
  `VisionExtractor.describe_figure()`로 설명을 붙인다(VLM, PPTX/DOCX와 동일한 개별 성능 저하
  정책 — 이미지 하나가 실패해도 문서 전체는 실패하지 않음).

**검증의 한계**: 이 환경에는 실제 한컴 워드로 생성된 `.hwpx` 샘플 파일이 없다. 테스트 fixture도
`python-hwpx` 자체로 만든 것이라(`HwpxDocument.new()` + `add_heading`/`add_table`/
`add_picture`), 검증은 "같은 라이브러리로 왕복(round-trip)"이지 독립적인 실파일 검증이
아니다. 실제 한컴 파일과의 호환성은 `python-hwpx` 업스트림의 자체 테스트 주장에 의존한다 —
라이브 검증은 실제 hancom 생성 파일을 구해서 별도로 진행해야 한다.
