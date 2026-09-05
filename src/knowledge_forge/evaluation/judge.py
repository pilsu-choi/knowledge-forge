"""LLM-as-judge Answer Groundedness / Citation Accuracy scoring
(wiki/search-evaluation.md (B)) - offline, batch, no gold relevance labels
needed: reads a `search_log` row's (query, answer, evidence) and scores how
well the answer's claims are actually supported by the evidence it
gathered.

One `ChatProvider.send()` call does claim extraction AND evidence matching
together - the design doc sketches these as two conceptual steps, but a
single structured-output call gets the model looking at claims and evidence
in the same context for one round trip instead of two, which is simpler to
run as a batch over many logs. Reuses whatever `ChatProvider` the caller
passes in - the same "no separate judge model/config" choice
`search/planner.py::decompose_query` makes for query decomposition.

A judge is itself an LLM and can misjudge - this is a low-cost approximation
standard in RAG evaluation, not ground truth (see wiki/search-evaluation.md
(B)'s "한계"). `claims` is persisted in full (not just failures) precisely so
a human can sample it to check the judge's own accuracy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from knowledge_forge.chat_provider import ChatProvider, Message
from knowledge_forge.storage.eval_store import SearchLog

logger = logging.getLogger(__name__)

_VALID_VERDICTS = {"supported", "unsupported", "contradicted"}

_JUDGE_SYSTEM_PROMPT = """당신은 RAG 답변 평가자다. 주어진 질문(query)/답변(answer)/근거
(evidence, 검색 도구가 실제로 반환한 원본 결과)를 보고, 답변이 근거로 실제로 뒷받침되는지
채점한다.

절차:
1. 답변을 개별 사실 주장(claim)으로 분해한다. 인사말/되묻기/명백한 일반 상식은 claim으로
   세지 않는다. 답변 자체가 실패를 인정하는 경우(예: "근거를 찾지 못했습니다")처럼 검증 가능한
   주장이 하나도 없으면 claims를 빈 배열로 반환할 것.
2. 각 claim을 evidence와 대조해 verdict를 매긴다:
   - "supported": evidence의 내용으로 뒷받침됨.
   - "unsupported": evidence 어디에도 이 claim을 뒷받침하는 내용이 없음.
   - "contradicted": evidence가 이 claim과 반대되는 내용을 담고 있음.
   reason에는 한 문장으로 판단 근거(또는 근거 없음)를 설명할 것.
3. 답변이 특정 근거(path, knowledge_object_id 등)를 명시적으로 인용했다면, 그 인용이
   evidence 안에 실제로 존재하는 항목을 가리키는지 citation_valid(true/false)에 표시할 것 -
   인용이 없는 claim은 citation_valid를 null로 둘 것.

다른 설명 없이 아래 JSON 객체 하나만 출력할 것(마크다운 코드펜스 없이):
{"claims": [{"text": "...", "verdict": "supported|unsupported|contradicted", "reason": "...", "citation_valid": true}]}"""


@dataclass
class JudgeResult:
    groundedness_score: float
    citation_precision: float | None
    claims: list[dict]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[: -len("```")]
    return text.strip()


def _parse_judge_response(raw_text: str) -> list[dict] | None:
    """`None` means the response wasn't usable at all (should not be scored
    - the caller leaves the log unscored for a later retry); an empty list
    means the judge explicitly found no checkable claims (a legitimate
    groundedness_score=1.0, not a failure)."""
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("claims"), list):
        return None
    return [
        claim
        for claim in parsed["claims"]
        if isinstance(claim, dict) and isinstance(claim.get("text"), str) and claim.get("verdict") in _VALID_VERDICTS
    ]


def _score_claims(claims: list[dict]) -> tuple[float, float | None]:
    if not claims:
        return 1.0, None  # no checkable claims - nothing to be ungrounded about
    supported = sum(1 for claim in claims if claim["verdict"] == "supported")
    groundedness_score = supported / len(claims)

    cited = [claim for claim in claims if claim.get("citation_valid") is not None]
    citation_precision = (sum(1 for claim in cited if claim["citation_valid"]) / len(cited)) if cited else None
    return groundedness_score, citation_precision


async def judge_search_log(log: SearchLog, provider: ChatProvider) -> JudgeResult | None:
    """Scores one `SearchLog`, or `None` if the judge call itself failed or
    returned something unparseable - the caller (`knowledge-forge eval`)
    should leave that log unscored rather than write a fabricated score, so
    the next batch run picks it back up via
    `EvalStore.list_unscored_search_logs`."""
    prompt = json.dumps(
        {"query": log.query, "answer": log.answer, "evidence": log.evidence}, ensure_ascii=False
    )
    try:
        turn = await provider.send([Message(role="user", content=prompt)], [], _JUDGE_SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - one bad judge call must not abort a batch eval pass
        logger.warning("groundedness judge call failed for log_id=%s: %s", log.log_id, exc)
        return None

    claims = _parse_judge_response(turn.text or "")
    if claims is None:
        logger.warning("groundedness judge returned an unparseable response for log_id=%s", log.log_id)
        return None

    groundedness_score, citation_precision = _score_claims(claims)
    return JudgeResult(groundedness_score=groundedness_score, citation_precision=citation_precision, claims=claims)


_DECLINED_SYSTEM_PROMPT = """당신은 답변 분류기다. 질문과 그에 대한 답변을 보고, 그 답변이
**"지식 베이스에 해당 정보가 없다"고 결론지었는지**만 판정한다.

declined=true인 경우:
- "관련 문서를 찾지 못했습니다", "지식 베이스에 없습니다" 등 정보 부재를 결론으로 말한 답변.
- 검색 과정을 길게 설명하더라도 **결론이 "없다"**이면 true다.
- 대신 다른 주제의 문서를 안내했더라도, 물어본 것 자체는 없다고 했으면 true다.

declined=false인 경우:
- 질문이 물어본 내용을 실제로 답한 경우(부분적이거나 틀렸더라도).
- 정보 부재를 말하지 않고 추측이나 일반 상식으로 답한 경우.

JSON만 출력할 것: {"declined": true} 또는 {"declined": false}"""


async def judge_answer_declined(query: str, answer: str, provider: ChatProvider) -> bool | None:
    """이 답변이 "해당 정보가 없다"로 결론지었는가. 판정할 수 없으면 `None`.

    왜 `judge_search_log`의 claims로는 안 되나 - 실측에서 드러난 것: null 질의에
    에이전트가 **옳게** "육아휴직 문서를 찾지 못했습니다"라고 답했는데, 그 답변이
    자기 검색 과정을 설명하며 "벡터/렉시컬 모두 0건", "SQL 조회도 0건" 같은
    **검증 가능한 주장**을 담고 있었다. 그래서 claims가 비지 않았고, "claims가
    비면 실패를 인정한 것"이라는 추론이 정확히 반대 결론을 냈다(none_correct 0.0).

    claims의 공백은 "검증할 주장이 없다"는 뜻이지 "없다고 답했다"는 뜻이 아니다.
    두 개념은 겹칠 뿐 같지 않으므로 따로 묻는다 - null/negative 유형은 골든셋
    186개 중 12개뿐이라 호출 한 번을 더 쓰는 비용이 작다."""
    prompt = json.dumps({"query": query, "answer": answer}, ensure_ascii=False)
    try:
        turn = await provider.send([Message(role="user", content=prompt)], [], _DECLINED_SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - 판정 실패가 run 전체를 죽이면 안 된다
        logger.warning("declined judge call failed for query=%r: %s", query[:60], exc)
        return None
    try:
        payload = json.loads(_strip_code_fence(turn.text or ""))
    except (json.JSONDecodeError, TypeError):
        logger.warning("declined judge returned an unparseable response for query=%r", query[:60])
        return None
    value = payload.get("declined") if isinstance(payload, dict) else None
    return value if isinstance(value, bool) else None
