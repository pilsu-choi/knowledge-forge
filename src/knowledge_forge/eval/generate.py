"""골든셋 생성 - 샘플링, LLM 생성, 자동 게이트(wiki/evaluation.md "게이트").

kind별 경로:
- lookup: 원문(chunk grain은 token_count>=80 청크, KO grain은 entity가 아닌 KO + evidence 청크)을
  source_type 라운드로빈 + 문서당 2개로 뽑아 LLM이 질의/정답(+ chunk grain은 인용문)을 쓴다.
  규칙 게이트 → 라벨 확장 게이트를 통과해야 채택.
- exhaustive: 정답 문서 집합은 SQL 술어(소스, 확장자, 슬랙 채널, 기간)가 정하고 LLM은 문장만 쓴다.
- none: LLM이 없는 주제를 제안하고, 핵심 term 문서빈도 0 + 같은 grain 벡터 최고 유사도 <
  `KF_VECTOR_MIN_SCORE`일 때만 채택.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from difflib import SequenceMatcher
from itertools import zip_longest
from typing import Any, TypeVar

from knowledge_forge.chat_provider import ChatProvider, Message
from knowledge_forge.config import settings
from knowledge_forge.eval import gather_bounded
from knowledge_forge.eval.goldenset import (
    ALL_ROWS,
    Evidence,
    GoldenItem,
    GoldenSet,
    Grain,
    corpus_fingerprint,
    normalize_text,
    quote_in,
    ranked_ids,
    resolve_gold,
)
from knowledge_forge.exceptions import EvalError
from knowledge_forge.search.constraints import _extract_json_object
from knowledge_forge.search.retrieve import _sql_literal, _term_document_frequency_sql, retrieve
from knowledge_forge.search.service import lexical_search, vector_search
from knowledge_forge.storage import PipelineStore

T = TypeVar("T")

MIX = {"lookup": 0.7, "exhaustive": 0.15}
PER_DOCUMENT_CAP = 2
MIN_CHUNK_TOKENS = 80
MAX_POSITIVES = 5
EXPANSION_TOP = 8
COPY_MIN_CHARS = 25
MIN_QUERY_CHARS = 12
EXHAUSTIVE_SIZE = (2, 50)
MIN_ANCHOR_CHARS = 15
NONE_ATTEMPTS_PER_ITEM = 6
_SOURCE_LABELS = {"google_drive": "구글 드라이브", "gmail": "메일", "slack": "슬랙", "notion": "노션", "local_file": "로컬 파일"}

# --- 규칙 게이트 ---------------------------------------------------------------------
# 한글 앞에서 `\b`가 경계로 안 잡히는 함정(검색 전략 세션 기록) 때문에 ASCII lookaround를 쓴다.
_RAW_ID_RE = re.compile(r"(?<![A-Za-z0-9])(?:[CGDU](?=[A-Z0-9]*\d)[A-Z0-9]{8,12}|[0-9a-f]{16,})(?![A-Za-z0-9])")
_DEICTIC_RE = re.compile(r"(?:^|\s)(?:이|그|해당|위|본)\s?(?:문서|프로젝트|파일|메일|회의|대화|자료|채널|스레드|보고서)")
_WH_RE = re.compile(
    r"누가|누구|무엇|뭐|무슨|어떤|어떻게|어떠|언제|어디|왜|몇|얼마|어느|(?<![A-Za-z])(?:what|who|when|where|why|how|which)(?![A-Za-z])",
    re.IGNORECASE,
)
_YES_NO_RE = re.compile(
    r"(?:맞나요|맞습니까|인가요|입니까|있나요|없나요|했나요|되나요|나요|습니까|까요|니까|는가|을까|죠|지요)\s*\?*$"
    r"|^(?:is|are|was|were|do|does|did|can|could|has|have|will|should)\s",
    re.IGNORECASE,
)
# 원문에 답이 없다는 "정답"(스모크에서 "소유자에 대한 정보는 원문에 명시되어 있지 않습니다"가 통과했다).
_UNANSWERABLE_RE = re.compile(r"명시(?:되어|돼)\s?있지\s?않|언급(?:되어|돼)?\s?있지\s?않|알\s?수\s?없|정보(?:가|는)?\s?없|나와\s?있지\s?않")


def target_mix(n: int) -> dict[str, int]:
    """기본 300 = lookup 210 / exhaustive 45 / none 45, 다른 n은 같은 비율."""
    lookup, exhaustive = round(n * MIX["lookup"]), round(n * MIX["exhaustive"])
    return {"lookup": lookup, "exhaustive": exhaustive, "none": max(0, n - lookup - exhaustive)}


def rule_gate(query: str, source_text: str = "") -> str | None:
    """거부 사유 또는 None. 원문 복사(최장 공통 부분문자열)는 원문이 있을 때만 본다."""
    q = normalize_text(query)
    if len(q) < MIN_QUERY_CHARS:
        return "too_short"
    if _RAW_ID_RE.search(q):
        return "raw_id"
    if _DEICTIC_RE.search(q):
        return "deictic"
    if "여부" in q or (not _WH_RE.search(q) and _YES_NO_RE.search(q)):
        return "yes_no"
    if source_text:
        source = normalize_text(source_text)
        if SequenceMatcher(None, q, source, autojunk=False).find_longest_match(0, len(q), 0, len(source)).size >= COPY_MIN_CHARS:
            return "copies_source"
    return None


def anchor_quote(text: str, quote: str) -> str | None:
    """LLM 인용문을 원문에 실제로 있는 구절로 고정한다. 그대로 있으면 그대로, 구두점·공백만 조금 바뀐 경우는
    원문과의 최장 공통 부분(`MIN_ANCHOR_CHARS`자 이상)으로 대체, 아니면 None - 실행 시점 라벨 해석은 인용문이
    원문에 글자 그대로 있어야 한다."""
    if quote_in(text, quote):
        return quote.strip()
    q, t = normalize_text(quote), normalize_text(text)
    match = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return t[match.b : match.b + match.size].strip() if match.size >= MIN_ANCHOR_CHARS else None


def pool_candidate_ids(*id_lists: Sequence[str], cap: int) -> list[str]:
    """서로 다른 검색 arm의 id 리스트를 라운드로빈으로 섞어 중복 제거하고 `cap`개로 자른다 - 한
    arm이 상위를 독점해 다른 arm의 후보가 밀려나지 않게 한다."""
    pooled: list[str] = []
    for row in zip_longest(*id_lists):
        for candidate_id in row:
            if candidate_id and candidate_id not in pooled:
                pooled.append(candidate_id)
                if len(pooled) >= cap:
                    return pooled
    return pooled


def split_key(item: GoldenItem) -> str:
    """dev/test 분할 해시 입력 - lookup은 문서 id(chunk grain evidence의 첫 문서 = KO grain의
    `document_id`, 둘 다 lookup()이 채운다), exhaustive는 정답 문서 중 가장 앞선 id, none은 문서가
    없으므로 질의 자체를 쓴다. 문서 id 기준이라 문서당 최대 `PER_DOCUMENT_CAP`개인 lookup들이
    같은 쪽으로 몰려 dev/test 사이에 새지 않는다(번호 짝홀 분할의 누수, wiki/search-quality-review-
    2026-09-15.md 3.4)."""
    if item.document_id:
        return item.document_id
    if item.document_ids:
        return min(item.document_ids)
    return item.query


def split_for(key: str) -> str:
    """`key`의 sha256 해시 첫 바이트로 dev/test를 결정 - 같은 키는 항상 같은 쪽에 떨어진다."""
    return "test" if hashlib.sha256(key.encode("utf-8")).digest()[0] % 2 else "dev"


# --- LLM ---------------------------------------------------------------------------

_LOOKUP_SYSTEM = """당신은 사내 검색 평가용 질의를 만든다. 주어진 원문(source)을 읽어야만 답할 수 있는 질의 하나를 만든다.
규칙:
- 사람이 검색창에 칠 법한 한국어 질문. 원문 문장을 베끼지 말고 바꿔 말할 것(연속 20자 이상 복사 금지).
- 예/아니오 질문 금지. 누가/무엇/언제/얼마/어떻게처럼 구체적인 답이 있는 질문.
- 질의만 보고도 어느 문서·대화 얘기인지 좁혀지도록 고유한 단서(프로젝트명, 고객사, 시스템명, 문서 제목 등)를 넣을 것.
- 채널 id·해시 같은 원시 식별자는 넣지 말 것. 일반 상식으로 답할 수 있는 질문 금지.
- answer는 한두 문장의 짧은 정답. 원문에 구체적인 답이 없으면 질문을 만들지 말고 query를 빈 문자열로.
{quote_rule}다른 설명 없이 JSON 객체 하나만 출력: {schema}"""

_QUOTE_RULE = "- quote는 정답의 근거가 되는 원문 구절을 한 글자도 바꾸지 않고 그대로 옮긴 것(20~150자).\n"

_EXPAND_SYSTEM = """검색 평가 라벨을 검수한다. 질문(query)과 정답(answer)이 있고, 검색이 찾은 후보(candidates)가 있다.
각 후보가 이 질문에 대해 같은 정답을 담고 있는지 판정한다. 주제만 비슷하고 정답 내용이 없으면 아니다.
{quote_rule}다른 설명 없이 JSON 객체 하나만 출력: {schema}"""

_EXPAND_QUOTE_RULE = "- 같은 정답을 담은 후보마다 quote에 그 근거 구절을 후보 원문에서 한 글자도 바꾸지 않고 옮길 것.\n"

_EXHAUSTIVE_SYSTEM = """검색 평가용 "전수 조회" 질의를 만든다. 주어진 조건(condition)에 해당하는 문서를 전부 찾아 달라는
자연스러운 한국어 요청 하나를 쓴다. 조건을 빠짐없이 반영하고 없는 조건을 덧붙이지 말 것. 슬랙 채널은 채널 이름으로,
기간은 "YYYY년 M월"로 쓸 것. 다른 설명 없이 JSON 객체 하나만 출력: {"query": "..."}"""

_NONE_SYSTEM = """검색 평가용 "정답 없음" 질의를 만든다. 코퍼스는 아래 제목(titles)의 사내 문서들이다.
같은 회사·업무 도메인에서 그럴듯하지만 이 코퍼스에는 없을 구체적인 주제(특정 제도, 시스템, 장비, 행사, 규정 등)에
대한 한국어 질문 하나를 쓴다. 제목에 나오는 고객사·프로젝트 이름은 쓰지 말 것.
key_terms는 그 주제를 특정하는 고유한 단어 1~2개만 - "분석", "트렌드", "프로젝트", "기능", "절차" 같은 흔한 단어는 넣지 말 것.
이미 만든 질의(avoid)와 겹치지 말 것. 예/아니오 질문 금지.
다른 설명 없이 JSON 객체 하나만 출력: {"query": "...", "key_terms": ["..."]}"""


async def _ask_json(provider: ChatProvider, system: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    turn = await provider.send([Message(role="user", content=json.dumps(payload, ensure_ascii=False))], [], system)
    return _extract_json_object(turn.text or "")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# --- 샘플링 ---------------------------------------------------------------------------

_CHUNK_SAMPLE_SQL = """
SELECT c.chunk_id, c.document_id, s.source_type, 'title: ' || COALESCE(d.title, '') || E'\\n' || c.text, c.text
  FROM chunks c JOIN documents d ON d.document_id = c.document_id JOIN sources s ON s.source_id = d.source_id
 WHERE c.token_count >= {min_tokens}
 ORDER BY md5(c.chunk_id || {seed})
"""

_KO_SAMPLE_SQL = """
SELECT knowledge_object_id, document_id, source_type, text, text FROM (
SELECT ko.knowledge_object_id, ko.document_id, s.source_type,
       'type: ' || ko.type || E'\\nsummary: ' || ko.summary || E'\\ncontent: ' || ko.content::text || E'\\nevidence:\\n' ||
       COALESCE((SELECT string_agg(c.text, E'\\n---\\n' ORDER BY c.sequence_index) FROM chunks c
                  WHERE c.chunk_id IN (SELECT e->>'chunk_id' FROM jsonb_array_elements(ko.evidence) e)), '') AS text,
       md5(ko.knowledge_object_id || {seed}) AS shuffle
  FROM knowledge_objects ko JOIN sources s ON s.source_id = ko.source_id
 WHERE ko.type <> 'entity'
   AND NOT EXISTS (SELECT 1 FROM knowledge_objects n WHERE n.supersedes = ko.knowledge_object_id)
) ko ORDER BY shuffle
"""


def stratify(rows: Sequence[tuple[Any, ...]], per_document: int = PER_DOCUMENT_CAP) -> list[tuple[Any, ...]]:
    """(id, document_id, source_type, 프롬프트 원문, 게이트 원문) 행을 source_type 라운드로빈 + 문서당 상한으로 편다.
    입력 순서(시드 셔플)는 각 source_type 안에서 유지된다."""
    by_type: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        by_type.setdefault(row[2], []).append(row)
    queues = [iter(by_type[key]) for key in sorted(by_type)]
    taken: Counter[str] = Counter()
    out: list[tuple[Any, ...]] = []
    while queues:
        for queue in list(queues):
            for row in queue:
                if taken[row[1]] < per_document:
                    taken[row[1]] += 1
                    out.append(row)
                    break
            else:
                queues.remove(queue)
    return out


# --- 생성기 ---------------------------------------------------------------------------


class _Generator:
    def __init__(self, store: PipelineStore, provider: ChatProvider, grain: Grain, seed: int, concurrency: int) -> None:
        self.store, self.provider, self.grain, self.seed, self.concurrency = store, provider, grain, seed, concurrency
        self.rejects: Counter[str] = Counter()

    async def _rows(self, sql: str) -> list[tuple[Any, ...]]:
        rows, _ = await self.store.execute_readonly_query(sql, ALL_ROWS)
        return rows

    async def fill(self, target: int, candidates: Sequence[T], make: Callable[[T], Awaitable[GoldenItem | str]]) -> list[GoldenItem]:
        """후보를 순서대로 소비하며 `target`개가 찰 때까지 배치로 만든다(거부 사유는 `rejects`에 센다)."""
        accepted: list[GoldenItem] = []
        position = 0
        while len(accepted) < target and position < len(candidates):
            batch = candidates[position : position + math.ceil((target - len(accepted)) * 1.5)]
            position += len(batch)
            for result in await gather_bounded((make(c) for c in batch), self.concurrency):
                if isinstance(result, str):
                    self.rejects[result] += 1
                elif len(accepted) < target:
                    accepted.append(result)
        return accepted

    # lookup ---------------------------------------------------------------------------

    async def lookup_candidates(self) -> list[tuple[Any, ...]]:
        if self.grain == "chunk":
            sql = _CHUNK_SAMPLE_SQL.format(min_tokens=MIN_CHUNK_TOKENS, seed=_sql_literal(str(self.seed)))
        else:
            sql = _KO_SAMPLE_SQL.format(seed=_sql_literal(str(self.seed)))
        return stratify(await self._rows(sql))

    async def lookup(self, row: tuple[Any, ...]) -> GoldenItem | str:
        # 프롬프트에는 제목을 붙여 보이지만, 인용문·원문 복사 게이트는 청크 본문만 본다 - 제목에서 딴
        # 인용문은 어느 청크에도 없어 생성 직후부터 stale이다.
        target_id, document_id, _source_type, prompt_text, source_text = row[:5]
        chunk = self.grain == "chunk"
        schema = '{"query": "...", "answer": "...", "quote": "..."}' if chunk else '{"query": "...", "answer": "..."}'
        payload = await _ask_json(
            self.provider,
            _LOOKUP_SYSTEM.format(quote_rule=_QUOTE_RULE if chunk else "", schema=schema),
            {"source": prompt_text},
        )
        if payload is None:
            return "bad_llm_output"
        query, answer, quote = _text(payload.get("query")), _text(payload.get("answer")), _text(payload.get("quote"))
        if not answer:
            return "bad_llm_output"
        if _UNANSWERABLE_RE.search(answer):
            return "unanswerable"
        reason = rule_gate(query, source_text)
        if reason:
            return reason
        item = GoldenItem(id="", query=query, answer=answer, kind="lookup", split="dev", document_id=document_id)
        if chunk:
            anchored = anchor_quote(source_text, quote)
            if anchored is None:
                return "quote_not_in_source"
            item.evidence = [Evidence(document_id=document_id, quote=anchored)] + await self._same_content_evidence(document_id, anchored)
        else:
            item.knowledge_object_ids = [target_id]
        return await self.expand_labels(item)

    async def _same_content_evidence(self, document_id: str, quote: str) -> list[Evidence]:
        """같은 content_hash인 다른 문서의 동일 인용문은 판정 없이 정답에 넣는다(복제 수집본)."""
        rows = await self._rows(
            f"""SELECT DISTINCT c.document_id, c.text FROM documents d
  JOIN sources s ON s.source_id = d.source_id
  JOIN sources s2 ON s2.content_hash = s.content_hash AND s2.source_id <> s.source_id
  JOIN documents d2 ON d2.source_id = s2.source_id
  JOIN chunks c ON c.document_id = d2.document_id
 WHERE d.document_id = {_sql_literal(document_id)}"""
        )
        documents = dict.fromkeys(doc for doc, text in rows if quote_in(text, quote))
        return [Evidence(document_id=doc, quote=quote) for doc in documents]

    async def expand_labels(self, item: GoldenItem) -> GoldenItem | str:
        """서로 독립적인 검색 arm(전체 파이프라인, 벡터 단독, 어휘 단독)에서 모은 후보 중 같은 답을
        담은 것을 정답에 더한다. `retrieve()` 상위만 보면 그 파이프라인이 놓친 정답 청크가 영구히
        오답으로 채점되어 A/B가 현재 파이프라인에 유리해진다(풀링 편향,
        wiki/search-quality-review-2026-09-15.md 3장). 검색이 원래 정답을 못 찾아도 항목은 버리지
        않는다 - 그게 바로 평가가 잡아야 할 실패다."""
        ranked = await self._candidate_pool(item.query)
        if ranked:
            reason = await self._judge_candidates(item, ranked)
            if reason:
                return reason
        # 정답 수는 라벨 항목이 아니라 실행 시점에 해석될 grain 단위로 센다 - 짧고 흔한 인용문 하나가
        # 청크 수십 개로 풀리면 recall@5가 구조적으로 무너진다.
        gold, stale = await resolve_gold(item, self.grain, self.store)
        if gold is None:
            return stale or "no_labels"
        return "anchorless" if len(gold) > MAX_POSITIVES else item

    async def _candidate_pool(self, query: str) -> list[str]:
        """`retrieve()`(전체 파이프라인), `vector_search`(벡터 단독), chunk grain이면
        `lexical_search`(어휘 단독) 상위를 라운드로빈으로 섞어 `EXPANSION_TOP`개로 자른다 - 판정
        비용을 기존과 비슷하게 유지하면서 한 arm에만 있는 후보도 판정 대상에 들어오게 한다."""
        retrieved = await retrieve(query, store=self.store, use_llm_constraints=False, grain=self.grain)
        vector_hits = await vector_search(query, self.store, limit=EXPANSION_TOP, target_kind=self.grain)
        arms = [ranked_ids(retrieved.hits, self.grain), ranked_ids(vector_hits, self.grain)]
        if self.grain == "chunk":
            lexical_hits = await lexical_search(query, self.store, limit=EXPANSION_TOP)
            arms.append(ranked_ids(lexical_hits, self.grain))
        return pool_candidate_ids(*arms, cap=EXPANSION_TOP)

    async def _judge_candidates(self, item: GoldenItem, ranked: list[str]) -> str | None:
        """상위 후보 중 같은 답을 담았다고 LLM이 판정한 것을 `item` 정답에 더한다(거부 사유 또는 None)."""
        ids = ", ".join(_sql_literal(i) for i in ranked)
        if self.grain == "chunk":
            rows = await self._rows(f"SELECT chunk_id, document_id, text FROM chunks WHERE chunk_id IN ({ids})")
            known = {(ev.document_id, ev.quote) for ev in item.evidence}
            texts = {cid: (doc, text) for cid, doc, text in rows if not any(doc == d and quote_in(text, q) for d, q in known)}
        else:
            rows = await self._rows(
                "SELECT knowledge_object_id, document_id, 'summary: ' || summary || E'\\ncontent: ' || content::text "
                f"FROM knowledge_objects WHERE knowledge_object_id IN ({ids})"
            )
            texts = {kid: (doc, text) for kid, doc, text in rows if kid not in item.knowledge_object_ids}
        candidates = [cid for cid in ranked if cid in texts]
        if not candidates:
            return None
        chunk = self.grain == "chunk"
        schema = '{"matches": [{"i": 0, "quote": "..."}]}' if chunk else '{"matches": [{"i": 0}]}'
        payload = await _ask_json(
            self.provider,
            _EXPAND_SYSTEM.format(quote_rule=_EXPAND_QUOTE_RULE if chunk else "", schema=schema),
            {
                "query": item.query,
                "answer": item.answer,
                "candidates": [{"i": i, "text": texts[cid][1][:1500]} for i, cid in enumerate(candidates)],
            },
        )
        if payload is None or not isinstance(payload.get("matches"), list):
            return "bad_llm_output"
        for match in payload["matches"]:
            index = match.get("i") if isinstance(match, dict) else None
            if not isinstance(index, int) or not 0 <= index < len(candidates):
                continue
            document_id, text = texts[candidates[index]]
            if not chunk:
                item.knowledge_object_ids.append(candidates[index])
            elif anchored := anchor_quote(text, _text(match.get("quote"))):
                evidence = Evidence(document_id=document_id, quote=anchored)
                if evidence not in item.evidence:
                    item.evidence.append(evidence)
            else:
                return "positive_without_quote"
        return None

    # exhaustive -----------------------------------------------------------------------

    async def predicates(self) -> list[tuple[dict[str, str], str]]:
        """(조건 설명, WHERE 절) 후보 - 가족(확장자/슬랙 채널/소스+월/소스)마다 셔플 후 라운드로빈."""
        base = "FROM documents d JOIN sources s ON s.source_id = d.source_id"
        month = "date_trunc('month', COALESCE(s.modified_at, s.collected_at) AT TIME ZONE 'Asia/Seoul')"
        extension = r"lower(substring(d.title from '\.([A-Za-z0-9]{2,5})$'))"
        families: list[list[tuple[dict[str, str], str, int]]] = [
            [
                ({"source": "google_drive", "file_extension": ext}, f"s.source_type = 'google_drive' AND lower(d.title) LIKE {_sql_literal('%.' + ext)}", n)
                for ext, n in await self._rows(f"SELECT {extension}, COUNT(*) {base} WHERE s.source_type = 'google_drive' GROUP BY 1")
                if ext
            ],
            [
                ({"source": "slack", "channel": f"#{name}"}, f"s.source_type = 'slack' AND s.metadata->>'channel_name' = {_sql_literal(name)}", n)
                for name, n in await self._rows(f"SELECT s.metadata->>'channel_name', COUNT(*) {base} WHERE s.source_type = 'slack' GROUP BY 1")
                if name
            ],
            [
                (
                    {"source": source, "period": f"{start:%Y-%m}"},
                    f"s.source_type = {_sql_literal(source)} AND {month} = {_sql_literal(f'{start:%Y-%m-01}')}::timestamp",
                    n,
                )
                for source, start, n in await self._rows(f"SELECT s.source_type, {month}, COUNT(*) {base} GROUP BY 1, 2")
            ],
            [
                ({"source": source}, f"s.source_type = {_sql_literal(source)}", n)
                for source, n in await self._rows(f"SELECT s.source_type, COUNT(*) {base} GROUP BY 1")
            ],
        ]
        rng = random.Random(self.seed)
        lo, hi = EXHAUSTIVE_SIZE
        pools = [[(desc, where) for desc, where, n in family if lo <= n <= hi] for family in families]
        for pool in pools:
            rng.shuffle(pool)
        return [pool[i] for i in range(max(map(len, pools), default=0)) for pool in pools if i < len(pool)]

    async def exhaustive(self, predicate: tuple[dict[str, str], str]) -> GoldenItem | str:
        condition, where = predicate
        sql = f"SELECT d.document_id FROM documents d JOIN sources s ON s.source_id = d.source_id WHERE {where} ORDER BY 1"
        labeled = {**condition, "source": _SOURCE_LABELS.get(condition["source"], condition["source"])}
        payload = await _ask_json(self.provider, _EXHAUSTIVE_SYSTEM, {"condition": labeled})
        query = _text(payload.get("query")) if payload else ""
        reason = rule_gate(query) if query else "bad_llm_output"
        if reason:
            return reason
        document_ids = [row[0] for row in await self._rows(sql)]
        answer = f"조건 {json.dumps(condition, ensure_ascii=False)}에 해당하는 문서 {len(document_ids)}건"
        return GoldenItem(id="", query=query, answer=answer, kind="exhaustive", split="dev", predicate_sql=sql, document_ids=document_ids)

    # none -----------------------------------------------------------------------------

    async def none(self, attempt: int, avoid: list[str]) -> GoldenItem | str:
        titles = await self._rows(
            f"SELECT title FROM documents WHERE title IS NOT NULL ORDER BY md5(document_id || {_sql_literal(f'{self.seed}:{attempt}')}) LIMIT 40"
        )
        payload = await _ask_json(self.provider, _NONE_SYSTEM, {"titles": [t for (t,) in titles], "avoid": avoid[-20:]})
        if payload is None:
            return "bad_llm_output"
        query = _text(payload.get("query"))
        terms = [t.strip() for t in payload.get("key_terms") or [] if isinstance(t, str) and t.strip()]
        reason = rule_gate(query) if terms else "bad_llm_output"
        if reason or query in avoid:
            return reason or "duplicate"
        df_rows = await self._rows(_term_document_frequency_sql(terms, project_id=None, since=None, until=None))
        if any(df for _index, df in df_rows):
            return "none_term_in_corpus"
        hits = await vector_search(query, self.store, limit=1, target_kind=self.grain)
        if hits and float(hits[0].get("score") or 0.0) >= settings.vector_min_score:
            return "none_vector_too_similar"
        return GoldenItem(id="", query=query, kind="none", split="dev")


async def generate_golden(
    store: PipelineStore,
    provider: ChatProvider,
    *,
    grain: Grain,
    n: int = 300,
    seed: int = 0,
    concurrency: int = 8,
    version: str | None = None,
) -> tuple[GoldenSet, Counter[str]]:
    """골든셋 하나와 게이트별 거부 수. 해당 grain 임베딩이 0건이면 라벨 확장·none 게이트가 무의미해서 거부한다."""
    corpus = await corpus_fingerprint(store)
    # none 게이트는 이 시점의 `KF_VECTOR_MIN_SCORE`로 채택 여부를 정한다(`_Generator.none`) - 하한을
    # 올리기만 해도 none_correct가 오르는데(검색 품질이 아니라 임계값을 잰다), 골든셋에 그 값을
    # 남겨야 나중에 무엇을 기준으로 만들었는지 알 수 있다. `corpus`는 `dict[str, Any]`라 기존
    # 골든셋 파일(이 키가 없음)도 그대로 로드된다.
    corpus["none_gate"] = {"vector_min_score": settings.vector_min_score}
    if corpus["embeddings"][grain] == 0:
        raise EvalError(
            f"{grain} 임베딩이 0건이다(KF_EMBEDDING_MODEL={settings.embedding_model}). "
            f"`knowledge-forge embed --embedding-source {grain}`로 먼저 백필할 것."
        )
    mix = target_mix(n)
    gen = _Generator(store, provider, grain, seed, concurrency)
    lookups = await gen.fill(mix["lookup"], await gen.lookup_candidates(), gen.lookup)
    exhaustive = await gen.fill(mix["exhaustive"], await gen.predicates(), gen.exhaustive)
    avoid: list[str] = []

    async def _none(attempt: int) -> GoldenItem | str:
        result = await gen.none(attempt, avoid)
        if not isinstance(result, str):
            avoid.append(result.query)
        return result

    nones = await gen.fill(mix["none"], range(mix["none"] * NONE_ATTEMPTS_PER_ITEM), _none)
    items = [
        item.model_copy(update={"id": f"{kind}-{i:03d}", "split": split_for(split_key(item))})
        for kind, group in (("lookup", lookups), ("exhaustive", exhaustive), ("none", nones))
        for i, item in enumerate(group)
    ]
    created_at = datetime.now(timezone.utc)
    golden = GoldenSet(
        version=version or f"{grain}-{created_at:%Y%m%d%H%M}",
        grain=grain,
        created_at=created_at,
        corpus=corpus,
        items=items,
    )
    return golden, gen.rejects
