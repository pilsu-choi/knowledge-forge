"""골든셋 자동 생성기 (wiki/search-evaluation.md (C)의 `eval-gen`) - 손으로 쓴
v0을 사람이 확장하는 대신, 인덱스에 이미 있는 원문 청크에서 "이 청크로만 답할 수
있는 질문"을 LLM으로 만들어 `origin: "synthetic"` 골든셋 **초안**을 낸다.

설계에서 되돌리기 어려운 선택들:

- **원천은 `chunks.text`(원문 청크)이지 KnowledgeObject가 아니다.** KO에서 뽑으면
  추출 품질이 골든셋 품질의 상한이 되고(추출이 놓친 사실은 질문이 될 수 없다),
  `--no-extract` 경로로 인제스트한 코퍼스에서는 아예 돌지 않는다. 청크를 원천으로
  두면 "KO 임베딩 vs 청크 임베딩"(`KF_EMBEDDING_SOURCE`) A/B도 같은 골든셋으로
  잰다.
- **임베딩에 의존하지 않는다.** 샘플링은 SQL 필터 + 결정적 층화이고 검증은
  `retrieve`의 렉시컬/메타데이터 경로만으로도 돈다 - `OPENAI_API_KEY` 없는
  환경에서 생성 파이프라인이 멈추면 안 된다.
- **자동 필터는 버리지 않고 표시한다.** 생성된 질의를 다시 검색해 정답 문서가
  상위 k에 없으면 "너무 어렵거나 모호함" 후보이지만, 그게 곧 나쁜 질의라는 뜻은
  아니다(검색이 못 푸는 질의야말로 평가에 필요하다). 그래서 `notes`에
  `[review:hard]`를 남기고 사람 검수로 넘긴다. 반대로 **기계적으로 확실히 나쁜
  것**(청크 문장을 그대로 베낌, 너무 짧음, 중복, 이 파이프라인이 정답 모양을
  만들 수 없는 유형)만 버린다.
- **출력은 항상 draft다.** `golden_version`에 draft 표식을 강제하는 건 CLI 쪽
  책임이고, 여기서는 사람 검수 전이라는 사실이 `origin`/`notes`에 남는다.

DB도 LLM도 직접 만들지 않는다 - `ChatProvider`, 후보 청크 목록, retriever가
전부 주입이라 fake만으로 단위 테스트가 된다(`tests/evaluation/test_gen.py`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from knowledge_forge.chat_provider import ChatProvider, Message
from knowledge_forge.evaluation.golden import (
    QUERY_TYPES,
    GoldenExpected,
    GoldenQuery,
    GoldenSet,
    IndexSnapshot,
    compute_source_hash,
)
from knowledge_forge.evaluation.ir_runner import RetrieveFn, default_retrieve_fn
from knowledge_forge.storage.eval_store import ChunkSample

if TYPE_CHECKING:
    from knowledge_forge.storage import PipelineStore

logger = logging.getLogger(__name__)

# 청크 하나를 근거로 만들 수 있는 유형만 허용한다. `golden.py`의 로더가 유형별
# 정답 모양을 강제하는데(집계는 `count`, set_query는 `exhaustive`, negative/null은
# `none=true`, relative_temporal/latest는 `as_of`, ambiguous/multihop/비교는 문서
# 2개 이상), 그 모양들은 청크 하나에서 나올 수 없다 - 만들면 로더가 거부하거나
# 더 나쁘게는 "정답이 한 문서뿐인 set_query" 같은 거짓 라벨이 된다. 그 유형들은
# 여전히 사람이 쓰거나(v0가 그렇게 만들어졌다) 별도 생성기가 필요하다.
SINGLE_CHUNK_TYPES: frozenset[str] = frozenset(
    {
        "factoid",
        "specific_document_qa",
        "navigational",
        "whole_document_summary",
        "exact_match",
        "entity_centric",
        "temporal",
        "metadata_filter",
        "parent_child",
        "conversation_thread",
        "long_range_context",
    }
)

# 자동 검증에서 정답 문서를 상위 k에 못 찾은 질의에 붙는 표식. 사람 검수 때
# `grep '[review:hard]'`로 모아 볼 수 있게 고정 문자열로 둔다.
REVIEW_FLAG_HARD = "[review:hard]"

# 청크 문장을 그대로 베낀 질의를 거르는 기준: 질의와 청크의 최장 공통 부분
# 문자열이 이 길이(문자) 이상이면 복사로 본다. 한국어는 조사 때문에 토큰 단위
# 겹침이 자연스럽게 커서, 토큰 자카드보다 "연속으로 몇 글자를 그대로 옮겼나"가
# 복사를 훨씬 잘 가른다.
DEFAULT_COPY_MAX_SUBSTRING = 25

_GEN_SYSTEM_PROMPT = """당신은 사내 지식 검색 시스템의 **검색 평가용 질의**를 만드는 사람이다.
문서에서 잘라낸 청크(원문 일부) 하나가 주어진다. 그 청크를 근거로, 검색 평가에 쓸 질의를 만든다.

규칙:
1. **그 청크만 보면 답할 수 있는 질문**이어야 한다. 청크에 없는 정보를 묻지 말 것.
2. 청크의 문장을 그대로 베끼지 말 것. 사람이 검색창에 칠 법한 자연스러운 한국어 질문으로 다시 쓸 것.
3. 청크 밖의 맥락 없이도 검색으로 찾을 수 있어야 한다 - "이 문서에서", "위 표에서", "그 프로젝트"처럼
   가리키는 말 대신, 청크에 나오는 고유명사/식별자/제목/날짜를 질문 안에 직접 넣을 것.
4. **개인 이름을 쓰지 말 것** (역할·조직·채널·프로젝트·제품명만). 이 결과 파일은 리포에 커밋된다.
5. 답이 청크 안에 짧고 명확하게 있는 질문을 선호할 것. 애매하거나 주관적인 질문("어떻게 생각하는가")은 만들지 말 것.
6. 질의 유형(query type)은 아래 목록에서만 고른다. `types`는 복수 라벨(해당되는 것 전부),
   `primary_type`은 그중 가장 대표적인 하나이며 반드시 `types`에 포함돼야 한다.

유형 목록과 뜻:
- factoid: 청크 안의 사실 하나를 묻는 질문
- specific_document_qa: 문서를 지목하고(제목/파일명) 그 안의 내용을 묻는 질문
- navigational: 특정 문서 자체를 찾는 질문(제목/파일명으로)
- whole_document_summary: 문서 전체 요약을 요구하는 질문
- exact_match: 에러코드/식별자/URL/따옴표 구절 등 표면형이 답인 질문
- entity_centric: 조직·프로젝트·제품 등 고유명사를 중심으로 한 질문
- temporal: 절대 날짜/기간이 조건인 질문(예: "2025년 3월")
- metadata_filter: 소스 종류·채널·메일 제목·파일 형식 등 메타데이터가 조건인 질문
- parent_child: 문서 안의 특정 절/섹션을 짚어 묻는 질문
- conversation_thread: 슬랙 채널/메일 스레드의 대화 내용을 묻는 질문
- long_range_context: 긴 문서에서 여러 부분을 함께 봐야 하는 질문

다른 설명 없이 아래 JSON 객체 하나만 출력할 것(마크다운 코드펜스 없이):
{"queries": [{"query": "...", "primary_type": "factoid", "types": ["factoid"], "answer": "청크에 근거한 짧은 정답", "notes": "선택적 보충 설명"}]}
청크가 질의를 만들기에 부적합하면(내용이 없거나, 목차/서명/머리말뿐이면) {"queries": []}를 출력할 것."""


@dataclass
class GenStats:
    """생성 파이프라인이 각 단계에서 무엇을 얼마나 걸렀는지 - CLI가 그대로
    출력하고, `--json`에도 들어간다. 사람 검수자가 "왜 20개를 요청했는데
    11개만 나왔나"를 다시 물어보지 않아도 되게 하는 게 목적이다."""

    candidates: int = 0
    sampled_chunks: int = 0
    sampled_by_source_type: dict[str, int] = field(default_factory=dict)
    llm_calls: int = 0
    llm_failures: int = 0
    raw_queries: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    accepted: int = 0
    verified: int = 0
    flagged_hard: int = 0

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "sampled_chunks": self.sampled_chunks,
            "sampled_by_source_type": dict(sorted(self.sampled_by_source_type.items())),
            "llm_calls": self.llm_calls,
            "llm_failures": self.llm_failures,
            "raw_queries": self.raw_queries,
            "rejected": dict(sorted(self.rejected.items())),
            "accepted": self.accepted,
            "verified": self.verified,
            "flagged_hard": self.flagged_hard,
        }


# --- 층화 샘플링 ---------------------------------------------------------------


def stratified_sample(
    candidates: Sequence[ChunkSample],
    *,
    n: int,
    seed: int = 0,
    max_per_document: int = 2,
) -> list[ChunkSample]:
    """`sources.source_type`(gdrive/slack/gmail)별로 골고루 `n`개를 고른다.

    비례 배분이 아니라 **라운드로빈**이다: 개발 DB는 google_drive 246 / slack 197 /
    gmail 59처럼 소스가 크게 치우쳐 있어서, 비례로 뽑으면 소수 소스가 한두 개만
    들어와 유형별 집계가 사실상 한 소스의 성능이 된다.

    `max_per_document`는 한 문서가 샘플을 독점하는 걸 막는다 - 청크 936개가 문서
    263개에 몰려 있어, 문서 상한이 없으면 청크가 많은 긴 문서 하나에서 절반이
    나온다.

    시드가 같으면 결과도 같다(`random.Random(seed)`, 입력 순서는 호출자가
    `ORDER BY chunk_id`로 고정한다)."""
    if n <= 0:
        return []
    buckets: dict[str, list[ChunkSample]] = {}
    for c in candidates:
        buckets.setdefault(c.source_type, []).append(c)
    rng = random.Random(seed)
    for chunks in buckets.values():
        rng.shuffle(chunks)

    per_document: dict[str, int] = {}
    picked: list[ChunkSample] = []
    order = sorted(buckets)
    cursors = dict.fromkeys(order, 0)
    while len(picked) < n:
        progressed = False
        for source_type in order:
            if len(picked) >= n:
                break
            chunks = buckets[source_type]
            i = cursors[source_type]
            while i < len(chunks):
                candidate = chunks[i]
                i += 1
                if per_document.get(candidate.document_id, 0) >= max_per_document:
                    continue
                per_document[candidate.document_id] = per_document.get(candidate.document_id, 0) + 1
                picked.append(candidate)
                progressed = True
                break
            cursors[source_type] = i
        if not progressed:
            break  # 모든 버킷이 소진됨 - 요청한 n보다 적게 돌려준다
    return picked


# --- LLM 응답 파싱 -------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[: -len("```")]
    return text.strip()


def _loads_object(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_generation_response(raw_text: str) -> list[dict] | None:
    """`None`은 "응답 자체가 못 쓸 것"(파싱 실패 - 그 청크는 실패로 세고 건너뛴다),
    빈 리스트는 "모델이 이 청크로는 질의를 못 만들겠다고 명시적으로 답함"이다.
    `judge.py::_parse_judge_response`와 같은 구분 - 네트워크/포맷 실패를 조용히
    '생성할 게 없었음'으로 기록하지 않기 위해서다.

    코드펜스를 벗긴 뒤에도 실패하면 **첫 `{`부터 마지막 `}`까지**를 한 번 더
    시도한다. judge와 달리 여기서는 그럴 값이 있다: 실측에서 로컬 `claude` CLI가
    JSON 앞뒤에 설명 한 줄을 붙이는 경우가 있었고, 그걸 실패로 세면 청크 하나가
    통째로 버려진다(judge가 만든 미채점 로그는 다음 배치에서 자동 재시도되지만,
    생성은 재시도 대상이 아니다)."""
    stripped = _strip_code_fence(raw_text)
    parsed = _loads_object(stripped)
    if parsed is None:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            parsed = _loads_object(stripped[start : end + 1])
    if parsed is None or not isinstance(parsed.get("queries"), list):
        return None
    return [item for item in parsed["queries"] if isinstance(item, dict)]


# --- 자동 필터 -----------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_query(text: str) -> str:
    """중복 판정용 정규화 - 공백 축약 + 소문자 + 끝 물음표 제거. 같은 질문을
    두 청크에서 각각 만들어내는 일이 흔해서 필요하다."""
    return _WS_RE.sub(" ", text).strip().strip("?？").lower()


def longest_common_substring_len(a: str, b: str) -> int:
    """두 문자열의 최장 공통 부분 문자열 길이. 청크가 최대 수천 자, 질의가 수십
    자라 O(len(a)*len(b))로 충분하고, 롤링 해시를 쓸 이유가 없다(질의 쪽을 짧은
    축으로 두고 1행 DP로 돈다)."""
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    previous = [0] * (len(a) + 1)
    best = 0
    for cb in b:
        current = [0] * (len(a) + 1)
        for i, ca in enumerate(a, start=1):
            if ca == cb:
                current[i] = previous[i - 1] + 1
                if current[i] > best:
                    best = current[i]
        previous = current
    return best


def reject_reason(
    item: dict,
    chunk: ChunkSample,
    seen: Iterable[str],
    *,
    min_query_chars: int = 12,
    copy_max_substring: int = DEFAULT_COPY_MAX_SUBSTRING,
) -> str | None:
    """기계적으로 확실히 나쁜 생성물만 걸러 그 사유를 돌려준다(통과면 `None`).

    "검색이 못 찾는다"는 여기서 거르지 않는다 - 그건 `verify_queries`가
    `notes`에 플래그로 남기고 사람이 판단한다."""
    query = str(item.get("query") or "").strip()
    if len(query) < min_query_chars:
        return "too_short"
    if normalize_query(query) in set(seen):
        return "duplicate"
    primary = str(item.get("primary_type") or "").strip()
    if primary not in SINGLE_CHUNK_TYPES:
        # QUERY_TYPES에는 있지만 청크 하나로 정답 모양을 만들 수 없는 유형까지
        # 여기서 함께 걸린다(aggregation/set_query/negative/...). `types`가 비어
        # 있는 건 거르지 않는다 - `build_golden_query`가 primary_type으로 채운다.
        return "unsupported_type" if primary in QUERY_TYPES else "unknown_type"
    if longest_common_substring_len(query, chunk.text) >= copy_max_substring:
        return "copied_from_chunk"
    return None


def build_golden_query(
    item: dict,
    chunk: ChunkSample,
    *,
    query_id: str,
) -> GoldenQuery:
    """필터를 통과한 생성물 하나를 골든셋 항목으로. 정답은 **청크가 속한 문서**
    하나이고(`document_ids`), 청크 id는 엄격 채점용으로 덧붙인다 - `golden.py`가
    설명하는 대로 채점 단위는 document_id여야 KO grain / 청크 grain 양쪽에서
    같은 파일로 채점된다."""
    types = [t for t in (str(x).strip() for x in item.get("types") or []) if t in SINGLE_CHUNK_TYPES]
    primary = str(item["primary_type"]).strip()
    if primary not in types:
        types = [primary, *types]
    answer = str(item.get("answer") or "").strip()
    extra = str(item.get("notes") or "").strip()
    notes = f"자동생성(eval-gen). 근거 청크 {chunk.chunk_id} ({chunk.source_type})."
    if answer:
        notes += f" 정답: {answer}"
    if extra:
        notes += f" / {extra}"
    return GoldenQuery(
        id=query_id,
        query=str(item["query"]).strip(),
        primary_type=primary,
        types=list(dict.fromkeys(types)),
        expected=GoldenExpected(document_ids=[chunk.document_id], chunk_ids=[chunk.chunk_id]),
        origin="synthetic",
        notes=notes,
        source_hash=compute_source_hash([chunk.content_hash]),
    )


# --- 생성 / 검증 ---------------------------------------------------------------


def _chunk_prompt(chunk: ChunkSample, per_chunk: int) -> str:
    return json.dumps(
        {
            "n_queries": per_chunk,
            "source_type": chunk.source_type,
            "document_title": chunk.title or "",
            "uri": chunk.uri,
            "chunk_text": chunk.text,
        },
        ensure_ascii=False,
    )


async def generate_for_chunk(chunk: ChunkSample, provider: ChatProvider, per_chunk: int = 2) -> list[dict] | None:
    """청크 하나에 대해 `ChatProvider.send()` 1회. `judge.py`와 같은 이유로
    provider 예외를 삼키고 `None`을 돌려준다 - 청크 하나의 실패가 배치 전체를
    날리면 안 되고, 실패 개수는 `GenStats.llm_failures`로 보고된다."""
    try:
        turn = await provider.send(
            [Message(role="user", content=_chunk_prompt(chunk, per_chunk))], [], _GEN_SYSTEM_PROMPT
        )
    except Exception as exc:  # noqa: BLE001 - 청크 하나의 실패가 배치를 중단시키면 안 된다
        logger.warning("eval-gen 질의 생성 호출 실패 chunk_id=%s: %s", chunk.chunk_id, exc)
        return None
    items = parse_generation_response(turn.text or "")
    if items is None:
        logger.warning("eval-gen 질의 생성 응답을 파싱할 수 없음 chunk_id=%s", chunk.chunk_id)
    return items


async def verify_queries(
    queries: list[GoldenQuery],
    *,
    store: PipelineStore,
    retrieve_fn: RetrieveFn | None = None,
    verify_k: int = 10,
    concurrency: int = 4,
) -> int:
    """생성된 질의를 `search/retrieve.py::retrieve`로 되돌려 검색해, 정답 문서가
    상위 `verify_k`에 없으면 `notes`에 `[review:hard]`를 붙인다(제자리 수정).
    붙은 개수를 돌려준다.

    **버리지 않는 이유**: 검색이 못 찾는 질의를 골든셋에서 빼면 그 골든셋은 항상
    좋은 점수를 내는 자기충족 예언이 된다. 어려운 질의야말로 개선을 측정할 여지가
    있는 항목이고, 진짜로 모호해서 못 찾는 것과 검색이 약해서 못 찾는 것을 가르는
    건 사람 검수의 몫이다.

    검증은 provider 없이(규칙 기반 제약 추출만) 돈다 - 임베딩도 planner도 없는
    환경에서 결정적으로 재현되게 하기 위해서다."""
    fn = retrieve_fn if retrieve_fn is not None else default_retrieve_fn()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(q: GoldenQuery) -> bool:
        expected_ids = set(q.expected.document_ids)
        async with semaphore:
            try:
                result = await fn(
                    q.query,
                    store=store,
                    provider=None,
                    project_id=None,
                    now=q.as_of,
                    limit=verify_k,
                    use_llm_constraints=False,
                    strategies_override=None,
                )
            except Exception as exc:  # noqa: BLE001 - 검증 실패는 질의를 버릴 이유가 아니다
                q.notes += f" {REVIEW_FLAG_HARD} 자동 검증 실패: {exc!r}"
                return True
        ranked = list(result.document_ids)[:verify_k]
        rank = next((i + 1 for i, d in enumerate(ranked) if d in expected_ids), None)
        if rank is None:
            q.notes += f" {REVIEW_FLAG_HARD} 자동 검증: 정답 문서가 상위 {verify_k}에 없음(전략={','.join(result.strategies)})."
            return True
        q.notes += f" 자동 검증 rank={rank}."
        return False

    flags = await asyncio.gather(*(_one(q) for q in queries))
    return sum(1 for f in flags if f)


async def generate_golden(
    candidates: Sequence[ChunkSample],
    *,
    provider: ChatProvider,
    golden_version: str,
    n_chunks: int,
    per_chunk: int = 2,
    seed: int = 0,
    max_per_document: int = 2,
    max_queries: int | None = None,
    min_query_chars: int = 12,
    copy_max_substring: int = DEFAULT_COPY_MAX_SUBSTRING,
    concurrency: int = 4,
    project_id: str | None = None,
    index_snapshot: IndexSnapshot | None = None,
    store: PipelineStore | None = None,
    retrieve_fn: RetrieveFn | None = None,
    verify: bool = True,
    verify_k: int = 10,
    created_at: datetime | None = None,
) -> tuple[GoldenSet | None, GenStats]:
    """층화 샘플링 → LLM 생성 → 자동 필터 → (선택) 검색 검증까지 한 번에 돌고
    `GoldenSet` 초안과 통계를 돌려준다. 사람 검수는 이 함수 밖이다.

    `store=None`이거나 `verify=False`면 검증 단계를 건너뛴다 - 단위 테스트와
    `eval-gen --no-verify`가 이 경로를 쓴다.

    통과한 질의가 하나도 없으면 `GoldenSet` 대신 `None`을 돌려준다 - 로더가
    빈 골든셋을 거부하기도 하지만(`queries` min_length=1), 그보다 "왜 0개인가"를
    보여주는 `GenStats`를 예외로 날려버리지 않기 위해서다."""
    stats = GenStats(candidates=len(candidates))
    chunks = stratified_sample(candidates, n=n_chunks, seed=seed, max_per_document=max_per_document)
    stats.sampled_chunks = len(chunks)
    for c in chunks:
        stats.sampled_by_source_type[c.source_type] = stats.sampled_by_source_type.get(c.source_type, 0) + 1

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(chunk: ChunkSample) -> list[dict] | None:
        async with semaphore:
            return await generate_for_chunk(chunk, provider, per_chunk)

    results = await asyncio.gather(*(_guarded(c) for c in chunks))
    stats.llm_calls = len(chunks)

    seen: set[str] = set()
    per_type_seq: dict[str, int] = {}
    queries: list[GoldenQuery] = []
    for chunk, items in zip(chunks, results, strict=True):
        if items is None:
            stats.llm_failures += 1
            continue
        stats.raw_queries += len(items)
        for item in items:
            if max_queries is not None and len(queries) >= max_queries:
                break
            if not isinstance(item.get("query"), str):
                stats.reject("malformed")
                continue
            reason = reject_reason(
                item, chunk, seen, min_query_chars=min_query_chars, copy_max_substring=copy_max_substring
            )
            if reason is not None:
                stats.reject(reason)
                continue
            primary = str(item["primary_type"]).strip()
            per_type_seq[primary] = per_type_seq.get(primary, 0) + 1
            query_id = f"{golden_version}-{primary}-{per_type_seq[primary]:03d}"
            try:
                queries.append(build_golden_query(item, chunk, query_id=query_id))
            except ValueError as exc:  # pydantic 불변식 위반 - 생성물이 스키마를 못 맞춘 경우
                logger.warning("eval-gen 항목이 골든셋 스키마를 위반해 버림 chunk_id=%s: %s", chunk.chunk_id, exc)
                stats.reject("schema_invalid")
                continue
            seen.add(normalize_query(item["query"]))
    stats.accepted = len(queries)

    if verify and store is not None and queries:
        stats.flagged_hard = await verify_queries(
            queries, store=store, retrieve_fn=retrieve_fn, verify_k=verify_k, concurrency=concurrency
        )
        stats.verified = len(queries)

    if not queries:
        return None, stats

    golden = GoldenSet(
        golden_version=golden_version,
        created_at=created_at or datetime.now(timezone.utc),
        project_id=project_id,
        index_snapshot=index_snapshot or IndexSnapshot(documents=0, chunks=0, knowledge_objects=0),
        queries=queries,
    )
    return golden, stats
