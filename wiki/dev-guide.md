# 개발 가이드

## 셋업

의존성 관리는 [`uv`](https://docs.astral.sh/uv/)를 사용한다.

```bash
uv sync
```

## Anthropic 자격 증명

앱 자체에서 API 키를 별도로 관리하지 않는다 — Anthropic Python SDK가 아래 순서로 자동
탐색한다.

1. `ANTHROPIC_API_KEY` 환경변수
2. `ANTHROPIC_AUTH_TOKEN` 환경변수
3. `ant auth login`으로 저장된 OAuth 프로필

`ant auth status`로 현재 활성 자격 증명을 확인할 수 있다. `ClaudeExtractor`는 `anthropic.Anthropic()`을
인자 없이 생성하므로, 위 자격 증명 중 하나만 설정되어 있으면 별도 코드 수정 없이 동작한다.

## Knowledge Forge 자체 설정값

`KF_` 접두사를 가진 환경변수(`src/knowledge_forge/config.py`의 `Settings`):

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `KF_EXTRACTION_MODEL` | `claude-opus-5` | Knowledge Extraction에 사용할 모델. `--model` CLI 옵션으로도 override 가능 |
| `KF_MAX_TOKENS_PER_CHUNK` | `4000` | Adaptive Chunker의 청크당 최대 토큰 예산. `--max-tokens` CLI 옵션으로도 override 가능 |

## CLI 실행

```bash
uv run knowledge-forge extract <path> [--max-tokens 4000] [--model claude-opus-5] [--output out.jsonl] [--pretty]
```

- `<path>`: 단일 파일 또는 디렉토리(디렉토리면 재귀적으로 순회).
- `--output`: 생략 시 stdout에 JSONL(한 줄에 Knowledge Object 하나) 출력.
- `--pretty`: 들여쓴 JSON 배열로 출력(사람이 읽기용, 대량 처리에는 비권장).

예시:

```bash
uv run knowledge-forge extract tests/fixtures/sample.md --pretty
```

## 테스트

```bash
uv run pytest
```

- 모델/청커/파서/컬렉터 테스트는 네트워크나 API 키 없이 동작한다.
- Extraction 테스트는 `anthropic.Anthropic` 클라이언트를 모킹하므로 `ANTHROPIC_API_KEY` 없이도
  통과한다.
- `tests/test_pipeline_e2e.py`는 LLM을 모킹한 상태로 전체 파이프라인(Collector→Parser→
  Chunker→Extraction)을 실행해 provenance 체인까지 검증하는 스모크 테스트다.

## 새 Parser 추가하기

1. `src/knowledge_forge/parsers/`에 `Parser`를 상속한 클래스를 만든다.
2. `supported_extensions: ClassVar[set[str]]`에 담당할 확장자를 지정한다.
3. `parse(self, item: RawItem, source_ref: SourceRef) -> Document`를 구현해 공통 `Document`
   구조(Heading/TextBlock/TableElement/ListElement/ImageElement)로 변환한다.
4. `src/knowledge_forge/parsers/registry.py`의 `default_registry()`에 인스턴스를 등록한다.
5. `tests/parsers/`에 fixture 파일과 함께 테스트를 추가한다.

## 새 SaaS Collector 추가하기

1. `src/knowledge_forge/collectors/saas/`에 `SaaSCollector`를 상속한 클래스를 만든다.
2. 생성자에서 실제 SDK 클라이언트(예: `slack_sdk.WebClient`)를 주입받도록 설계한다.
3. `initial_sync()`/`incremental_sync(cursor)`/`get_cursor()`를 구현하고, 변경/삭제 감지는
   `RawItem.content_hash`/`RawItem.deleted`로 표현한다.
4. `SaaSCollector._paginate()` 헬퍼를 사용해 페이지네이션 API를 순회할 수 있다.
5. 다른 파이프라인 단계(Parser 이후)는 전혀 수정할 필요가 없다.
