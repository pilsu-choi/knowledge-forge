# Parser 지원 현황

모든 Parser는 `Parser` ABC(`parse(item, source_ref) -> Document`)를 구현하며,
`ParserRegistry`가 파일 확장자로 알맞은 Parser를 찾아 디스패치한다.

## 포맷별 현황

| 포맷 | 확장자 | 상태 | 텍스트 추출 | 표/이미지 추출 | 비고 |
|---|---|---|---|---|---|
| PDF | `.pdf` | ✅ 실제 동작 | 규칙 기반(`pdfplumber`) | **VLM 하이브리드** | 아래 "PDF 라이브러리 선택", [`vision-extraction.md`](vision-extraction.md) 참고 |
| DOCX | `.docx` | ✅ 실제 동작 | 규칙 기반(`python-docx`) | 규칙 기반 | 스타일명(`Heading N`) 기반 헤딩 감지 |
| Markdown | `.md` | ✅ 실제 동작 | 규칙 기반(`markdown-it-py`) | 규칙 기반 | GFM 테이블 포함 |
| TXT | `.txt` | ✅ 실제 동작 | 규칙 기반(표준 라이브러리) | - | 빈 줄 기준 단락 분리, 헤딩 감지 없음 |
| XLSX | `.xlsx` | ✅ 실제 동작 | - | 규칙 기반(`openpyxl`) | 시트별 `TableElement`, 셀 값이 이미 텍스트라 VLM 불필요 |
| PPTX | `.pptx` | ✅ 실제 동작 | 규칙 기반(`python-pptx`) | 표는 규칙 기반, **그림(Picture)은 VLM** | 네이티브 차트 객체는 스텁, 아래 참고 |
| Image | `.png`/`.jpg`/`.jpeg` | ✅ 실제 동작 | - | **VLM 전용** | OCR(`pytesseract`) 대체 — 시스템 `tesseract-ocr` 의존성 완전히 제거됨 |
| HWPX | `.hwpx` | 🚧 스텁 | - | - | 아래 "HWPX 리스크" 참고 |

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

### PPTX 네이티브 차트 (스코프 아웃)

PPTX 안의 표는 `python-pptx`로 셀 텍스트를 정확히 읽을 수 있어 규칙 기반을 유지한다(VLM은
이미 정확히 아는 값을 추측하게만 만든다). 그림(Picture) 도형은 `shape.image.blob`으로 원본
이미지 바이트를 바로 얻을 수 있어 VLM로 설명을 붙인다. 하지만 파워포인트 **네이티브 차트
객체**(그림이 아니라 차트 XML로 존재하는 도형)는 `python-pptx`에 래스터화(이미지로 렌더링)
API가 없어 이번 범위에서 처리하지 않는다 — LibreOffice headless 같은 외부 슬라이드 렌더링
백엔드가 있어야 하며, HWPX와 마찬가지로 문서화된 한계로 남긴다([`roadmap.md`](roadmap.md)).

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

## HWPX 리스크와 계획

HWPX(한글과컴퓨터 오피스 오픈 XML)는 리눅스 환경에서 사용할 수 있는 성숙한 순수 Python
라이브러리가 없다.

- `pyhwpx`: Windows에서 한글과컴퓨터의 "한글" 프로그램을 COM 자동화로 제어하는 방식이라
  리눅스 서버 환경에서 사용 불가.
- `hwp5`/`pyhwp`: 구버전 바이너리 포맷인 `.hwp`를 대상으로 하며, XML 기반의 신버전 `.hwpx`는
  지원하지 않는다.

**계획**: HWPX는 DOCX(OOXML)와 마찬가지로 XML 파트들을 담은 zip 컨테이너다. 따라서
`zipfile` + `lxml`로 HWPX 자체 XML 스키마(OWPML)를 직접 파싱하는 커스텀 파서를 작성하는 것이
현실적인 접근이다. 다만 이는 스키마 리버스엔지니어링이 필요한 별도 규모의 작업이라, 이번 MVP
범위에서는 스텁으로 남기고 [`roadmap.md`](roadmap.md)에 후속 작업으로 명시한다.
