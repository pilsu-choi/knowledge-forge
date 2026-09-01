# VLM 기반 Table/Image 추출

PDF/Image/PPTX/HWPX/DOCX 파서는 시각적 이미지 콘텐츠를 규칙 기반 API만으로 처리하지 않고,
`VisionExtractor`(`src/knowledge_forge/vision/extractor.py`)를 통해 Claude vision 호출로
추출한다(단, 이미 정확한 구조화 데이터가 존재하는 표·네이티브 차트는 규칙 기반을 유지 — 아래
"독립 이미지 파일, PPTX, HWPX, DOCX" 참고). 이 문서는 그 설계 배경과 동작을 다룬다.

## 왜 "실패 시 폴백"이 아니라 "항상 VLM"인가

일반적인 하이브리드 파싱은 "규칙 기반 파서를 먼저 시도하고, 실패하거나 결과가 빈약할 때만
VLM을 호출"하는 정책을 쓴다. Knowledge Forge는 사용자의 명시적 요청에 따라 **표/이미지는
항상 VLM으로** 보낸다. 이유는 규칙 기반 파서가 가장 취약한 지점이 바로 표/이미지이기
때문이다.

- `pdfplumber.extract_tables()`는 격자(grid) 휴리스틱이라 병합 셀, 여러 줄에 걸친 셀,
  선이 없는 표(borderless table)에서 쉽게 틀린다.
- 규칙 기반 파서는 애초에 차트/다이어그램이 "무엇을 말하는지" 서술할 방법이 없다 — 셀
  텍스트를 읽는 것과 시각적 정보를 해석하는 것은 다른 문제다.
- "실패했을 때만 VLM"으로 하면 규칙 기반 파서가 **그럴듯하지만 틀린** 결과를 낸 경우(표는
  추출됐지만 셀이 밀려 있는 등)를 감지하지 못해 VLM으로 넘어가지 못한다. 항상 VLM을 쓰면 이
  실패 모드 자체가 없다.

트레이드오프는 비용/지연이다 — 아래 "비용과 지연" 절 참고.

## `VisionExtractor`의 세 메서드

`src/knowledge_forge/vision/extractor.py`. 모두 `client.messages.create(...)`에 base64
PNG/JPEG `image` 콘텐츠 블록 + 텍스트 프롬프트(`vision/prompts.py`)를 실어 보내고, 응답
텍스트를 그대로 반환한다.

| 메서드 | 프롬프트 | 반환값 | 사용처 |
|---|---|---|---|
| `extract_table_markdown()` | `TABLE_PROMPT` | GFM 마크다운 표 | PDF의 감지된 표 영역 |
| `describe_figure()` | `FIGURE_PROMPT` | 서술형 문단(2~5문장) | PDF의 감지된 이미지 영역, PPTX의 Picture 도형 |
| `transcribe_image()` | `TRANSCRIBE_PROMPT` | 전사/설명 텍스트 | 독립 이미지 파일(`.png`/`.jpg`) 전체 |

`extract_table_markdown()`의 결과는 `vision/markdown_table.py`의 `parse_markdown_table()`로
파싱되어 `TableElement.rows`(기존 `list[list[str]]` 형태)에 그대로 들어간다 — 이 덕분에
Adaptive Chunker의 행 단위 테이블 분할([`adaptive-chunking.md`](adaptive-chunking.md))을
포함한 하위 파이프라인 전체가 표가 VLM에서 왔는지 규칙 기반 파서에서 왔는지 전혀 몰라도 된다.

재시도 정책은 `retry.py`의 `anthropic_retry()`를 Knowledge Extraction(`ClaudeExtractor`)과
공유한다(`RateLimitError`/`APIConnectionError`/`InternalServerError`에 지수 백오프).

이 문서가 설명하는 VLM 호출은 모두 `vision/factory.py`의 `get_vision_extractor()`를 거쳐
프로바이더 추상화 위에서 이뤄진다 — `KF_LLM_PROVIDER=openrouter`로 전환하면 여기 설명된
`VisionExtractor` 대신 `OpenRouterVisionExtractor`가 같은 세 메서드로 호출된다(모델/API
형식만 다름). 자세한 내용은 [`llm-providers.md`](llm-providers.md) 참고.

## PDF: 표/이미지 영역 크롭 → 렌더링 → VLM

`parsers/pdf.py`가 각 페이지에서:

1. `page.find_tables()`로 표를 찾는다 — 각 표는 `bbox`(x0, top, x1, bottom)를 갖는다.
2. `page.images`로 임베드된 이미지를 찾는다 — 마찬가지로 bbox를 갖는다.
3. 각 bbox를 `page.crop(bbox).to_image(resolution=150)`으로 크롭 + 래스터화해 PNG 바이트를
   만든다. **시스템 의존성이 전혀 없다** — `pdfplumber`가 내부적으로 쓰는 `pypdfium2`가 순수
   wheel 배포라 poppler/ghostscript 같은 OS 바이너리 설치가 필요 없다(이 환경에서 실제로
   검증됨).
4. 표는 `extract_table_markdown()` → `parse_markdown_table()` → `TableElement.rows`,
   이미지는 `describe_figure()` → `ImageElement.ocr_text`로 매핑한다.

### 텍스트 중복 방지: bbox 겹침 제외

표/이미지 영역의 텍스트는 `page.extract_text_lines()`로 뽑는 일반 텍스트 줄에도 그대로
포함되어 있다(예: 표의 셀 텍스트가 "Company Sales Growth" 같은 한 줄로 잡힘). 이걸 그대로
두면 같은 내용이 VLM 표/이미지 결과와 일반 `TextBlock` 양쪽에 중복 추출된다.

그래서 각 텍스트 줄의 bbox가 이미 처리된 표/이미지 bbox와 **50% 이상 면적이 겹치면** 그 줄을
일반 텍스트 추출 대상에서 제외한다(`_bbox_overlap_fraction`, `_COVERAGE_OVERLAP_THRESHOLD =
0.5`). 예를 들어 표 bbox가 `(238, 142, 374, 196)`이고 "A 100M 20%" 텍스트 줄의 bbox가 그
안에 완전히 들어간다면, 이 줄은 걸러지고 표는 VLM 결과로만 남는다 — 실제 테스트 픽스처
(`tests/parsers/test_pdf_vlm_hybrid.py`)로 검증되어 있다.

### VLM 실패 시 개별 성능 저하 (전체 문서 실패 아님)

VLM 호출이 재시도까지 다 소진하고 실패하면:

- **표**: `page.find_tables()`가 이미 찾아둔 `table.extract()`(pdfplumber 자체의 결정론적
  셀 추출)로 대체한다 — `extraction_method`가 `"vlm"` 대신 `"rule_based"`로 기록된다.
- **이미지**: `ImageElement(ocr_text=None, extraction_method=None)`으로 — 설명 없는 빈
  이미지 요소로 남는다.

어느 경우든 한 표/이미지의 실패가 나머지 페이지·문서 파싱을 막지 않는다 — 로그에
경고만 남기고 계속 진행한다.

## 독립 이미지 파일, PPTX, HWPX, DOCX

- **`parsers/image.py`**: `.png`/`.jpg`/`.jpeg` 전체를 `transcribe_image()`에 통째로 보낸다.
  VLM이 재시도까지 실패하면 이건 문서 전체(=이미지 하나)의 파싱 실패이므로 `ParsingError`를
  던진다 — PDF처럼 "일부만 저하"시킬 결정론적 대안이 없다(사진 전체를 대체할 규칙 기반 파서가
  없음).
- **`parsers/pptx.py`**: 표는 `python-pptx`가 셀 텍스트를 정확히 알고 있으므로 그대로 규칙
  기반을 쓴다(VLM은 이미 확정된 값을 추측하게만 만들 뿐 이득이 없음). Picture 도형은
  `shape.image.blob`/`shape.image.content_type`으로 원본 이미지 바이트를 바로 얻어
  `describe_figure()`에 보낸다 — 슬라이드를 통째로 렌더링할 필요가 없다. **네이티브 차트
  객체**(도형이 아니라 차트 XML)는 이 환경에 슬라이드 래스터화 백엔드가 없어 이미지로 만들 수
  없지만, `python-pptx`의 차트 데이터 API로 카테고리/시리즈/값을 결정론적으로 바로 읽을 수
  있어 VLM이 필요 없다(오히려 VLM으로 이미지를 "읽어서 추측"하는 것보다 정확) —
  `extraction_method="rule_based"`인 `TableElement`로 처리한다. 자세한 내용은
  [`parsers.md`](parsers.md) 참고.
- **`parsers/hwpx.py`**: PPTX와 동일한 정책 — 표(`doc.tables`)는 셀 텍스트가 정확해 규칙
  기반, 이미지(`doc.media.images`)는 `describe_figure()`로 VLM 처리. [`parsers.md`](parsers.md)의
  "HWPX: `python-hwpx` 재발견" 참고.
- **`parsers/docx.py`**: 표는 `python-docx`가 셀 텍스트를 정확히 알고 있으므로 규칙 기반.
  임베드 이미지는 `inline_shapes`에서 Picture 타입만 골라 OOXML 관계 API
  (`shape._inline.graphic.graphicData.pic.blipFill.blip.embed` →
  `document.part.related_parts[rId].blob`/`.content_type`)로 원본 바이트를 얻어
  `describe_figure()`에 보낸다 — PPTX Picture 도형과 동일한 개별 성능 저하 정책.

## Provenance: `bbox` / `extraction_method`

`models/document.py`의 `ElementBase`에 `bbox: tuple[float,float,float,float] | None`이
추가되었다(현재는 PDF만 채움 — 페이지 좌표계의 x0/top/x1/bottom). `TableElement`/
`ImageElement`에는 `extraction_method: Literal["rule_based","vlm"] | None`이 추가되어 이
요소가 어느 경로로 만들어졌는지 남는다. 둘 다 선택 필드라 기존 데이터/테스트와 호환된다.
이 필드들은 "이 Knowledge Object가 PDF 몇 페이지의 어느 영역에서, 어떤 방식으로 추출됐는가"를
역추적할 수 있게 해준다.

## 비용과 지연

표/이미지 하나당 Claude API 호출이 1회씩 추가된다 — Knowledge Extraction이 청크당 1회
호출하는 것과 별개로, 표/이미지가 많은 문서(재무 보고서, 슬라이드 덱)는 Phase 1의 순수 규칙
기반 파싱보다 훨씬 많은 API 호출을 만든다. 이는 사용자가 "정확도를 위해 항상 VLM"을 명시적으로
선택한 결과다. `VisionExtractor(model=...)`로 Knowledge Extraction과 마찬가지로 모델을
바꿀 수 있다(`claude-opus-5` 기본값, 대량 처리 시 `claude-sonnet-5`/`claude-haiku-4-5`로
비용을 낮추는 것도 선택지 — [`dev-guide.md`](dev-guide.md)의 비용 레버 설명 참고). 후속
최적화로 Batches API 전환은 [`roadmap.md`](roadmap.md)에 남겨둔다.
