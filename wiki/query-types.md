# 검색 유형별 전략 표 (Query Types)

`search-architecture.md`의 "① Query Understanding"이 **왜** 명시적 레이어가 필요한지를 다룬다면,
이 문서는 그 레이어가 잡아야 하는 **질의 유형 20종**을 하나씩 늘어놓고, 각 유형에 대해
"왜 임베딩 유사도만으로는 어려운가 / 어떤 검색 전략이 필요한가 / `search/constraints.py`의 어떤
플래그·필드가 이 유형을 잡는가 / 지금 이 저장소에서 어디까지 되는가"를 적는다.

핵심 관찰: 20종 중 **유사도 검색이 자연스럽게 잘 되는 유형은 Entity-centric/Ambiguous/일반 QA
정도**다. 나머지는 제목 조회, 메타데이터 필터, 정확 일치, COUNT, 정렬, exhaustive 수집, 문서
전체 읽기, 다단계 분해처럼 유사도와 무관하거나 유사도가 오히려 방해되는(top-k 절단이 정답을
잘라내는) 전략이 필요하다. 그래서 "질의 → 제약+의도 플래그 → 전략 목록"이 검색의 첫 단계다.

전략 이름(`document_lookup`/`lexical`/`metadata_filter`/`vector`/`latest`/`exhaustive`/`count`/
`whole_document`/`comparison`/`negative`/`multihop`)의 정의는 `search/constraints.py::suggest_strategies`
docstring과 `search-architecture.md` ①절의 계약 표를 따른다.

## 유형 표

| # | 유형 | 예시 질의 | 왜 유사도가 어려운가 | 필요한 전략 | `constraints.py`가 잡는 것 | 현재 상태 |
|---|---|---|---|---|---|---|
| 1 | **Navigational** (특정 문서 찾기) | "2025_SH_업무_보고서 요약", "매출현황.xlsx 보여줘" | 제목 토큰은 임베딩 공간에서 의미가 희박하고, 청크 임베딩은 제목이 아니라 본문을 대표한다. 제목이 정확히 일치하는 문서보다 본문이 비슷한 다른 문서가 위로 올 수 있다 | `document_lookup` (제목/파일명 lexical) → 그 문서 안에서 후속 전략 | `document_reference` (파일명, `_`/`-` 연결 토큰), `identifiers` | 플래그 추출 구현됨. 실행은 다른 에이전트가 만드는 `document_search`(제목 렉시컬)와 다음 단계 `retrieve.py`가 담당 |
| 2 | **Whole-document Summary** | "2025_SH_업무_보고서 요약해줘", "지난주 회의록 전문" | 요약은 top-k 청크로 못 한다 - 유사도가 높은 청크 몇 개는 문서의 일부일 뿐이고, "요약해줘" 자체는 어떤 청크와도 유사하지 않다 | `document_lookup` + `whole_document` (`read_document`로 본문 전체) | `wants_summary`, `wants_whole_document` (전문/원문 마커, 또는 문서 참조+요약) | 플래그 구현됨. `read_document` 도구는 다른 에이전트가 구현 중 |
| 3 | **Specific Document QA** | "매출현황.xlsx에서 3분기 합계는?" | 문서 범위를 먼저 좁혀야 한다 - 전체 코퍼스 유사도는 다른 문서의 비슷한 표를 가져온다 | `document_lookup` → 그 문서 내 `vector`/`lexical` | `document_reference` + `semantic_query` | 플래그 구현됨. 문서 범위 한정 검색은 `retrieve.py`에서 조합 예정 |
| 4 | **Temporal** (절대 시간) | "2025년 3월 보고서", "2025 Q3 이슈" | 임베딩은 "2025년 3월"과 "2024년 3월"을 거의 같은 벡터로 본다. 시간은 필터이지 유사도가 아니다 | `metadata_filter` (소스 타임스탬프 `[start, end)`) | `time_range` (`search/temporal.py` 재사용) | **구현됨** - `file_search`/`vector_search`의 `since`/`until`, `sql_search` |
| 5 | **Relative Temporal** | "지난달 회의 결정사항", "올해", "최근 3개월" | 4와 같음 + 기준 시각(`now`)에 따라 답이 바뀐다. LLM에게 맡기면 오늘 날짜를 모르거나 틀린다 | `metadata_filter` | `time_range` (올해/작년/재작년은 `constraints.py`가 보충) | **구현됨** - `now` 주입으로 평가 재현 가능 |
| 6 | **Metadata Filter** | "김철수가 작성한 보고서 중 장애 관련", "슬랙 개발 채널에서" | 작성자/채널/문서 종류는 본문 임베딩에 거의 안 실린다. "김철수"와 유사한 청크는 김철수를 *언급한* 청크지 김철수가 *쓴* 청크가 아니다 | `metadata_filter` (`sources.metadata`: gdrive name/mimeType, gmail subject/participants, slack channel/participants) + `vector` | `authors`, `participants`, `source_types`, `document_types` | 추출 구현됨. 저장소 필터는 `sql_search`로 우회 가능, 전용 파라미터는 `retrieve.py`에서 |
| 7 | **Exact Match** | "ERR_CONNECTION_RESET 관련 문서", "\"인증 방식을 OAuth로\" 문구" | 에러코드/ID/따옴표 구절은 의미가 없거나 표면형 자체가 답이다. 임베딩은 `ERR_CONNECTION_RESET`과 `ERR_CONNECTION_REFUSED`를 구분 못 한다 | `lexical` (substring/BM25) | `identifiers` (대문자 코드, `PRJ-2025-001`, `#1234`, 파일명), `quoted_phrases` | 추출 구현됨. `file_search`의 substring 매치가 lexical 역할, `router.py`도 같은 신호로 file_search 우선 |
| 8 | **Entity-centric** | "OKF 프로젝트 관련 논의" | 유사도가 비교적 잘 된다 - 다만 entity 이름이 약어/별칭이면 놓친다 | `vector` (+ `metadata_filter`로 project 한정) | `entities`, `project_hints` (규칙: "X 프로젝트", 작성자/참여자 이름; LLM 보강으로 고유명사 확장) | 규칙 추출 + LLM 보강 구현됨. 동의어/약어 확장은 미구현 |
| 9 | **Multi-hop** | "그 프로젝트의 성공 이유", "OKF 담당자가 작성한 회의록" | 두 번째 검색의 조건이 첫 검색의 답이다. 한 번의 유사도로는 조건 자체를 모른다 | `multihop` (`search/planner.py` 분해 → wave 실행) | `is_multihop` (규칙: 대명사/한정 표현 추정, LLM 보강 가능) | **구현됨** - `plan_search`/`planner.py`. 규칙 단계의 판정은 추정이라 recall 낮음 |
| 10 | **Cross-document Comparison** | "2024년과 2025년 사업계획의 차이" | 두 문서를 각각 찾아 나란히 놓아야 한다. 유사도 top-k는 한쪽 문서 청크만 몰아 가져오기 쉽다. 단일 시간 필터를 걸면 한쪽이 잘린다 | `comparison` (`comparison_targets`별 독립 검색) | `is_comparison`, `comparison_targets` (규칙: 접속 조사/vs 분리 + 공통 머리말 분배, LLM이 교체 가능). 대상별 시간 범위가 다르면 `time_range`는 비움 | 추출 구현됨. 대상별 병렬 검색은 `retrieve.py`에서 |
| 11 | **Aggregation** | "올해 프로젝트 몇 개 진행했어?", "장애 건수" | 답이 "숫자"라 어떤 청크와도 유사하지 않다. top-k 절단은 세는 것 자체를 불가능하게 한다 | `count` (필터 조건에서 SQL COUNT 직행) + `metadata_filter` | `wants_count`, `time_range`, `document_types` | 추출 구현됨. `sql_search`로 COUNT 가능(모델 판단), 자동 배선은 `retrieve.py` |
| 12 | **Set Query / Exhaustive** | "A 프로젝트 관련 문서 전부", "모든 회의록" | 유사도 검색은 상위 k개만 돌려주는 구조라 "전부"와 정의상 충돌한다 | `exhaustive` (페이지네이션으로 조건 만족 전부) | `wants_exhaustive` | 추출 구현됨. 페이지네이션 수집은 미구현 |
| 13 | **Negative** | "A 프로젝트에서 언급되지 않은 기술" | 유사도는 "있음"만 찾는다. "없음"은 대상 집합을 전부 모은 뒤 부재를 판정해야 증명된다 | `exhaustive` → `negative` (부재 판정) | `is_negative` (→ `exhaustive`도 자동 켜짐), `project_hints` | 추출 구현됨. 부재 판정 로직은 미구현 - 현재는 에이전트가 검색 결과를 보고 판단 |
| 14 | **Latest** | "가장 최근 업무보고서", "latest report from Alice" | "최근"은 유사도가 아니라 정렬 기준이다. 비슷한 보고서 10개 중 어느 게 최신인지 임베딩은 모른다 | `metadata_filter` → `latest` (타임스탬프 내림차순) | `wants_latest` ("최근 3개월"은 temporal이라 제외) | 추출 구현됨. `file_search`는 최신순 정렬을 하지만 vector 결과 재정렬은 `retrieve.py`에서 |
| 15 | **Version** | "사업계획서 최신 버전", "v2와 v3 차이" | 같은 문서의 버전들은 임베딩이 거의 같다 - 어느 버전인지는 인제스트 메타데이터(supersedes/버전)로만 안다 | `document_lookup` + `latest` (버전 체인 최신) 또는 `comparison` | `wants_latest`, `document_reference`, `is_comparison` | 인제스트 버전/supersedes 메타데이터는 다른 에이전트가 구현 중. 별도 플래그 없음 - latest/comparison으로 위임 |
| 16 | **Parent/Child** | "이 첨부파일이 붙은 메일", "이 시트가 속한 드라이브 폴더" | 문서 간 계층 관계는 본문 유사도와 무관하다 | `document_lookup` → 부모/자식 메타데이터 따라가기 | `document_reference` | 부모-자식 메타데이터는 다른 에이전트가 구현 중. 별도 플래그 없음 |
| 17 | **Conversation Thread** | "슬랙 개발 채널에서 어제 논의된 내용", "김철수와의 회의" | 대화는 메시지 단위로 쪼개져 있어 청크 하나로는 맥락이 없다. 채널/참여자는 메타데이터다 | `metadata_filter` (channel/participants/time) → 스레드 재구성 | `source_types` (slack/gmail), `participants`, `time_range` | 추출 구현됨. 스레드 맥락 재구성은 `context-reconstruction.md`(구현됨) |
| 18 | **Sequence** | "OAuth 전환 결정까지의 경위", "순서대로" | 시간 순서를 따라 여러 문서를 이어 읽어야 한다. 유사도는 순서를 모른다 | `vector` + 타임스탬프 오름차순 정렬 + `exhaustive` | 전용 플래그 없음 (`semantic_query` + `time_range`) | 미구현 - 에이전트가 반복 검색으로 대체 |
| 19 | **Long-range Context** | "이 계약서의 지불 조건과 위약 조항을 같이 봐줘" | 답이 한 문서의 멀리 떨어진 두 부분에 걸쳐 있다. top-k 청크는 한쪽만 가져온다 | `document_lookup` + `whole_document` 또는 문서 내 다중 `vector` | `document_reference`, `wants_whole_document` | 문서 전체 읽기(`read_document`) 구현 중. 문서 내 다중 청크 결합은 미구현 |
| 20 | **Ambiguous** | "인증 관련 그거", "그 프로젝트는?" | 정보가 부족하거나 대화 맥락에 의존한다. 유사도 검색은 되지만 뭘 찾는지가 불명확 | `vector` (+ 에이전트의 conversation-aware 복원/재질문) | `semantic_query`만 (제약 없음 → `vector` 단독) | 규칙 단계는 아무 플래그도 켜지 않아 vector로 흘러간다. 대화 맥락 복원은 에이전트 몫 |

## 읽는 법

- **"추출 구현됨 / 실행은 `retrieve.py`"**가 반복되는 이유: 이 단계는 의도적으로 *판단*만 하고
  *실행*은 하지 않는다. 판단이 결정론적 함수로 분리돼야 골든셋 평가가 "제약 추출이 틀렸나,
  검색이 틀렸나"를 가를 수 있다(평가 하네스는 2026-09-14 삭제, 재구현 예정 —
  [`roadmap.md`](roadmap.md) 참고). 실행은 다른 에이전트들이 동시에 만드는
  `document_search`/`read_document`/버전·부모-자식 메타데이터가 갖춰진 뒤 `retrieve.py`에서 한 번에
  조합한다.
- 유형은 서로 배타적이지 않다. "김철수가 2025년 3월에 작성한 보고서 전부"는 6+4+12이고,
  `suggest_strategies()`는 `["metadata_filter", "vector", "exhaustive"]`처럼 겹치는 전략을 그대로
  돌려준다.
- 15/16/18은 전용 플래그가 없다. 인제스트 쪽 메타데이터(버전 체인, 부모-자식)가 자리 잡은 뒤
  필요하면 `QueryConstraints`에 필드를 추가한다 - 지금 만들면 소비할 데이터가 없는 플래그가 된다.

## 관련 문서

- `search-architecture.md` ① Query Understanding - 왜 지금 명시적 레이어인가, 규칙/LLM 2단계, 전략 이름 계약
- `agent-search.md` - `file_search`/`vector_search`/`plan_search`/`sql_search` 도구
- `context-reconstruction.md` - 17 Conversation Thread의 맥락 재구성
