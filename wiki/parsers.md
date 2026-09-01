# Parser 지원 현황

모든 Parser는 `Parser` ABC(`parse(item, source_ref) -> Document`)를 구현하며,
`ParserRegistry`가 파일 확장자로 알맞은 Parser를 찾아 디스패치한다.

## 포맷별 현황

| 포맷 | 확장자 | 상태 | 라이브러리 | 라이선스 | 비고 |
|---|---|---|---|---|---|
| PDF | `.pdf` | ✅ 실제 동작 | `pdfplumber` | MIT | 아래 "PDF 라이브러리 선택" 참고 |
| DOCX | `.docx` | ✅ 실제 동작 | `python-docx` | MIT | 스타일명(`Heading N`) 기반 헤딩 감지 |
| Markdown | `.md` | ✅ 실제 동작 | `markdown-it-py` + `mdit-py-plugins` | MIT | GFM 테이블 포함 |
| TXT | `.txt` | ✅ 실제 동작 | 표준 라이브러리 | - | 빈 줄 기준 단락 분리, 헤딩 감지 없음 |
| XLSX | `.xlsx` | 🚧 스텁 | `openpyxl`(예정) | MIT | 시트 → `TableElement` 매핑 예정 |
| PPTX | `.pptx` | 🚧 스텁 | `python-pptx`(예정) | MIT | 슬라이드 제목 → `Heading`, 텍스트 → `TextBlock`, 표 → `TableElement` 예정 |
| Image | `.png`/`.jpg`/`.jpeg` | 🚧 스텁 | `Pillow` + `pytesseract`(예정) | MIT/Apache-2.0 | 시스템 `tesseract-ocr` 필요, 한국어는 `tesseract-ocr-kor` 언어팩 필요 |
| HWPX | `.hwpx` | 🚧 스텁 | 커스텀 파서(예정) | - | 아래 "HWPX 리스크" 참고 |

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
