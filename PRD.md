# PRD — Enterprise Knowledge Extraction Pipeline

## 1. 제품 목적

기업의 다양한 데이터를 수집하여 **문서 구조를 보존한 Semantic Chunk**로 변환하고, LLM을 통해 **구조화된 Knowledge Object**를 생성한다.

```text
Data Source
    ↓
Collector
    ↓
Parser
    ↓
Adaptive Chunker
    ↓
Knowledge Extraction
    ↓
Knowledge Object
```

## 2. 지원 데이터

### SaaS

* Slack
* Gmail
* Google Drive
* Notion

### File

* PDF
* DOCX
* XLSX
* PPTX
* HWPX
* Markdown
* TXT
* Image

---

## 3. 핵심 기능

### ① Collector

데이터 소스에서 원본 데이터를 수집한다.

* Initial Sync
* Incremental Sync
* 변경/삭제 감지
* Source Metadata 보존
* 원본 위치/URL 보존

### ② Parser

다양한 데이터를 공통 **Document 구조**로 변환한다.

* Text
* Heading
* Table
* List
* Image
* Page
* Metadata

기존 오픈소스 Parser/OCR을 적극 활용한다.

### ③ Adaptive Chunker

고정 Token 단위가 아닌 **문서 구조 기반 Chunking**을 수행한다.

```text
Document
 → Chapter
   → Section
     → Paragraph
       → Sentence
         → Line
```

상위 단위가 Context Window를 초과할 경우에만 하위 단위로 분할한다.

> **가능한 가장 큰 Semantic Unit을 유지한다.**

### ④ Knowledge Extraction

LLM을 사용하여 Chunk에서 Knowledge를 추출한다.

* Entity
* Relation
* Summary
* Key Facts
* Topic / Keyword
* Document Type

---

## 4. Knowledge Object

최종 결과는 표준화된 JSON 형태로 출력한다.

```json
{
  "source": {},
  "document": {},
  "chunk": {},
  "summary": "...",
  "facts": [],
  "entities": [],
  "relations": []
}
```

모든 Knowledge Object는 다음 Provenance를 유지한다.

```text
Source
  ↓
Document
  ↓
Chunk
  ↓
Knowledge Object
```

---

## 5. MVP 범위

```text
Slack ──────┐
Gmail ──────┤
GDrive ─────┤
Notion ─────┤
Files ──────┘
      ↓
   Collector
      ↓
    Parser
      ↓
Adaptive Chunker
      ↓
Knowledge Extraction
      ↓
Knowledge Object
```

### 제외 범위

* Vector DB
* Graph DB
* Ontology / OWL
* Search / RAG
* Agent
* Dashboard
* Chat UI

### 한 줄 정의

> **Slack, Gmail, Google Drive, Notion 및 다양한 파일을 수집하여 구조와 의미를 보존한 Knowledge Object로 변환하는 Enterprise Knowledge Ingestion & Extraction Pipeline.**
