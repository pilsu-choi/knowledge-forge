"""`eval-gen` 생성 파이프라인 - fake provider + fake retriever, DB 없음.

여기서 검증하는 건 검색 품질이나 LLM 출력의 좋고 나쁨이 아니라 **생성기 자신의
규칙**이다: 층화 샘플링이 소스/문서에 골고루 퍼지고 시드로 재현되는가, 자동
필터가 버려야 할 것만 버리는가(검색이 못 찾는 질의는 버리지 않고 표시만 하는가),
만들어진 항목이 `golden.py` 로더의 불변식을 그대로 통과하는가.
"""

import json
from datetime import datetime, timezone

import pytest

from knowledge_forge.chat_provider import AssistantTurn, ChatProvider
from knowledge_forge.evaluation.gen import (
    REVIEW_FLAG_HARD,
    SINGLE_CHUNK_TYPES,
    build_golden_query,
    generate_golden,
    longest_common_substring_len,
    normalize_query,
    parse_generation_response,
    reject_reason,
    stratified_sample,
    verify_queries,
)
from knowledge_forge.evaluation.golden import GoldenSet, compute_source_hash
from knowledge_forge.storage.eval_store import ChunkSample

CHUNK_TEXT = (
    "MLOps 개발계 보안취약점 패치는 총 3시간 이상 소요될 것으로 예상된다. "
    "MinIO 패치는 저장된 데이터 양에 비례해 시간이 늘어난다."
)


def _chunk(chunk_id="c1", document_id="d1", source_type="slack", text=CHUNK_TEXT, token_count=120) -> ChunkSample:
    return ChunkSample(
        chunk_id=chunk_id,
        document_id=document_id,
        source_id="s-" + document_id,
        source_type=source_type,
        content_hash="hash-" + document_id,
        text=text,
        token_count=token_count,
        title="MLOps 패치 계획",
        uri="slack://c/1",
    )


class _FakeProvider(ChatProvider):
    """호출마다 `responses`에서 하나씩 꺼내 돌려준다 - 청크별로 다른 생성 결과를
    주려면 순서에 의존해야 해서, 단일 문자열이 아니라 큐다."""

    def __init__(self, responses: list[str | Exception]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def send(self, messages, tools, system) -> AssistantTurn:
        self.calls.append(json.loads(messages[0].content))
        item = self._responses.pop(0) if self._responses else '{"queries": []}'
        if isinstance(item, Exception):
            raise item
        return AssistantTurn(text=item, tool_calls=[], stop_reason="end_turn")


def _response(*queries: dict) -> str:
    return json.dumps({"queries": list(queries)}, ensure_ascii=False)


def _item(query="MLOps 개발계 보안취약점 패치의 예상 소요 시간은?", primary="factoid", types=None, **kw) -> dict:
    return {"query": query, "primary_type": primary, "types": types or [primary], "answer": "3시간 이상", **kw}


# --- 층화 샘플링 ---------------------------------------------------------------


def test_stratified_sample_round_robins_source_types():
    """개발 DB는 소스별 청크 수가 크게 치우쳐 있다(gdrive≫gmail). 비례 배분이면
    소수 소스가 사실상 빠지므로 라운드로빈이어야 한다는 것."""
    candidates = [_chunk(f"g{i}", f"gd{i}", "google_drive") for i in range(10)]
    candidates += [_chunk("m0", "gm0", "gmail")]
    candidates += [_chunk(f"s{i}", f"sl{i}", "slack") for i in range(5)]

    picked = stratified_sample(candidates, n=6, seed=0)

    by_type: dict[str, int] = {}
    for c in picked:
        by_type[c.source_type] = by_type.get(c.source_type, 0) + 1
    assert len(picked) == 6
    assert by_type["gmail"] == 1  # 후보가 1개뿐 - 있는 만큼은 반드시 들어간다
    assert by_type["google_drive"] >= 2 and by_type["slack"] >= 2


def test_stratified_sample_is_deterministic_per_seed():
    """같은 시드는 같은 샘플 - 이게 깨지면 골든셋 초안을 재현할 수 없다."""
    candidates = [_chunk(f"c{i}", f"d{i}", "slack") for i in range(20)]

    a = [c.chunk_id for c in stratified_sample(candidates, n=5, seed=7)]
    b = [c.chunk_id for c in stratified_sample(candidates, n=5, seed=7)]
    c = [c.chunk_id for c in stratified_sample(candidates, n=5, seed=8)]

    assert a == b
    assert a != c


def test_stratified_sample_caps_chunks_per_document():
    """긴 문서 하나가 샘플을 독점하면 골든셋이 그 문서의 평가가 된다."""
    candidates = [_chunk(f"c{i}", "same-doc", "slack") for i in range(10)]

    picked = stratified_sample(candidates, n=10, seed=0, max_per_document=2)

    assert len(picked) == 2  # 후보가 소진되면 요청보다 적게 돌려준다


def test_stratified_sample_returns_empty_for_non_positive_n():
    assert stratified_sample([_chunk()], n=0, seed=0) == []


# --- 응답 파싱 -----------------------------------------------------------------


def test_parse_generation_response_distinguishes_unparseable_from_empty():
    """`None`(응답 자체가 못 쓸 것)과 `[]`(모델이 만들 게 없다고 답함)을 섞으면
    LLM/네트워크 실패가 조용히 '생성물 0개'로 기록된다 - judge.py와 같은 구분."""
    assert parse_generation_response("not json") is None
    assert parse_generation_response('{"nope": 1}') is None
    assert parse_generation_response('{"queries": []}') == []
    assert parse_generation_response('```json\n{"queries": [{"query": "q"}]}\n```') == [{"query": "q"}]


def test_parse_generation_response_recovers_json_wrapped_in_prose():
    """로컬 `claude` CLI가 JSON 앞뒤에 설명 한 줄을 붙이는 경우가 실측에서 나왔다 -
    그걸 실패로 세면 청크 하나가 통째로 버려진다(생성은 재시도 대상이 아니다)."""
    raw = '다음과 같이 만들었습니다.\n{"queries": [{"query": "q"}]}\n확인해 주세요.'

    assert parse_generation_response(raw) == [{"query": "q"}]


# --- 자동 필터 -----------------------------------------------------------------


def test_longest_common_substring_len():
    assert longest_common_substring_len("", "abc") == 0
    assert longest_common_substring_len("abcdef", "zzcdezz") == 3


def test_reject_reason_drops_copied_query():
    """청크 문장을 그대로 옮긴 '질의'는 검색 평가에서 정답을 그대로 넘겨주는
    것이라 무의미하다."""
    copied = _item(query="MLOps 개발계 보안취약점 패치는 총 3시간 이상 소요될 것으로 예상된다")

    assert reject_reason(copied, _chunk(), set()) == "copied_from_chunk"


def test_reject_reason_drops_short_duplicate_and_bad_types():
    chunk = _chunk()
    assert reject_reason(_item(query="짧음?"), chunk, set()) == "too_short"
    assert reject_reason(_item(), chunk, {normalize_query(_item()["query"])}) == "duplicate"
    # 청크 하나로는 정답 모양(count/exhaustive/none/as_of/문서 2개)을 만들 수 없는 유형
    assert reject_reason(_item(primary="aggregation"), chunk, set()) == "unsupported_type"
    assert reject_reason(_item(primary="설명형"), chunk, set()) == "unknown_type"


def test_reject_reason_accepts_a_normal_query():
    assert reject_reason(_item(), _chunk(), set()) is None


def test_single_chunk_types_exclude_shapes_one_chunk_cannot_produce():
    """`golden.py`가 유형별로 강제하는 정답 모양(count/exhaustive/none/as_of/문서
    2개 이상)은 청크 하나에서 나올 수 없다 - 허용 목록에 새 유형을 넣을 때 이
    테스트가 같이 걸려야 한다."""
    forbidden = {
        "aggregation",
        "set_query",
        "negative",
        "null",
        "relative_temporal",
        "latest",
        "ambiguous",
        "multihop",
        "cross_document_comparison",
    }
    assert not (SINGLE_CHUNK_TYPES & forbidden)


# --- 골든셋 항목 구성 ----------------------------------------------------------


def test_build_golden_query_matches_golden_schema():
    """정답은 청크가 아니라 **문서**로 잡는다(채점 단위가 document_id여야 KO/청크
    grain 양쪽에서 같은 파일로 채점된다). 청크 id는 엄격 채점용으로만 덧붙는다."""
    chunk = _chunk()
    q = build_golden_query(_item(types=["factoid", "entity_centric"]), chunk, query_id="v1-draft-factoid-001")

    assert q.origin == "synthetic"
    assert q.expected.document_ids == [chunk.document_id]
    assert q.expected.chunk_ids == [chunk.chunk_id]
    assert q.source_hash == compute_source_hash([chunk.content_hash])
    assert q.primary_type == "factoid" and set(q.types) == {"factoid", "entity_centric"}
    assert chunk.chunk_id in q.notes and "3시간 이상" in q.notes


def test_build_golden_query_forces_primary_type_into_types():
    """`golden.py`가 primary_type ∉ types를 거부하므로, 모델이 빠뜨려도 여기서
    메운다 - 라벨 하나 때문에 멀쩡한 질의를 버릴 이유가 없다."""
    q = build_golden_query(_item(primary="factoid", types=["entity_centric"]), _chunk(), query_id="x-1")
    assert q.primary_type in q.types


# --- 전체 파이프라인 -----------------------------------------------------------


async def test_generate_golden_produces_loadable_golden_set(tmp_path):
    """생성 결과가 `load_golden`이 그대로 읽는 파일이어야 한다 - 스키마 위반이
    사람 검수 단계가 아니라 여기서 걸려야 한다."""
    provider = _FakeProvider(
        [
            _response(_item(query="MLOps 개발계 보안취약점 패치 예상 소요 시간은 얼마인가?")),
            _response(_item(query="MinIO 패치 시간이 무엇에 비례해 늘어나는가?", primary="specific_document_qa")),
        ]
    )
    candidates = [_chunk("c1", "d1", "slack"), _chunk("c2", "d2", "gmail")]

    golden, stats = await generate_golden(
        candidates,
        provider=provider,
        golden_version="vtest-draft",
        n_chunks=2,
        per_chunk=1,
        verify=False,
        created_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )

    assert golden is not None
    assert stats.sampled_chunks == 2 and stats.accepted == 2 and stats.llm_failures == 0
    assert {q.id for q in golden.queries} == {"vtest-draft-factoid-001", "vtest-draft-specific_document_qa-001"}
    # 라운드트립: model_dump한 결과가 다시 GoldenSet으로 검증돼야 한다.
    GoldenSet.model_validate(json.loads(json.dumps(golden.model_dump(mode="json"))))
    # 프롬프트에 청크 원문이 실제로 들어갔는지(=KO가 아니라 chunks.text가 원천)
    assert provider.calls[0]["chunk_text"] == CHUNK_TEXT


async def test_generate_golden_counts_llm_failures_without_aborting():
    """청크 하나의 LLM 실패가 배치를 죽이면 20개짜리 생성이 통째로 날아간다."""
    provider = _FakeProvider([RuntimeError("boom"), _response(_item())])

    golden, stats = await generate_golden(
        [_chunk("c1", "d1", "slack"), _chunk("c2", "d2", "gmail")],
        provider=provider,
        golden_version="vtest-draft",
        n_chunks=2,
        per_chunk=1,
        verify=False,
    )

    assert stats.llm_failures == 1
    assert golden is not None and len(golden.queries) == 1


async def test_generate_golden_returns_none_when_everything_is_filtered():
    """전부 걸러졌을 때 예외 대신 (None, stats)를 주는 이유: '왜 0개인가'를
    설명하는 통계가 살아 있어야 한다."""
    provider = _FakeProvider([_response(_item(query="짧음?"))])

    golden, stats = await generate_golden(
        [_chunk()], provider=provider, golden_version="vtest-draft", n_chunks=1, per_chunk=1, verify=False
    )

    assert golden is None
    assert stats.rejected == {"too_short": 1}


async def test_generate_golden_respects_max_queries():
    provider = _FakeProvider([_response(_item(query="첫 번째 질문은 무엇인가?"), _item(query="두 번째 질문은 무엇인가?"))])

    golden, stats = await generate_golden(
        [_chunk()],
        provider=provider,
        golden_version="vtest-draft",
        n_chunks=1,
        per_chunk=2,
        max_queries=1,
        verify=False,
    )

    assert golden is not None and len(golden.queries) == 1


# --- 되돌리기 검증 -------------------------------------------------------------


class _FakeResult:
    def __init__(self, document_ids):
        self.document_ids = document_ids
        self.scores = [1.0] * len(document_ids)
        self.predicted_count = None
        self.strategies = ["lexical"]
        self.steps: list = []
        self.elapsed_ms = 1.0


async def test_verify_queries_flags_but_keeps_unretrievable_queries():
    """검색이 못 찾는 질의를 버리면 골든셋이 '항상 잘 나오는' 자기충족 예언이
    된다 - 표시만 하고 남긴다."""
    found = build_golden_query(_item(query="찾히는 질문은 무엇인가?"), _chunk("c1", "d1"), query_id="q-found")
    missed = build_golden_query(_item(query="못 찾는 질문은 무엇인가?"), _chunk("c2", "d2"), query_id="q-missed")

    async def _retrieve(query, **kwargs):
        return _FakeResult(["d1", "other"] if "찾히는" in query else ["other"])

    flagged = await verify_queries([found, missed], store=object(), retrieve_fn=_retrieve, verify_k=10)

    assert flagged == 1
    assert REVIEW_FLAG_HARD not in found.notes and "rank=1" in found.notes
    assert REVIEW_FLAG_HARD in missed.notes


async def test_verify_queries_survives_retrieval_errors():
    """검증 자체가 터져도(전략 하나가 죽는 등) 질의를 잃지 않고 검수 대상으로만
    넘긴다."""
    q = build_golden_query(_item(), _chunk(), query_id="q-1")

    async def _boom(query, **kwargs):
        raise RuntimeError("retrieval down")

    flagged = await verify_queries([q], store=object(), retrieve_fn=_boom, verify_k=5)

    assert flagged == 1 and REVIEW_FLAG_HARD in q.notes


@pytest.mark.parametrize("k", [1, 3])
async def test_verify_queries_honors_verify_k(k):
    """상위 k 밖의 정답은 '못 찾음'으로 본다 - k가 커지면 같은 질의가 통과한다."""
    q = build_golden_query(_item(), _chunk("c1", "d1"), query_id="q-1")

    async def _retrieve(query, **kwargs):
        return _FakeResult(["x", "y", "d1"])

    flagged = await verify_queries([q], store=object(), retrieve_fn=_retrieve, verify_k=k)

    assert flagged == (1 if k == 1 else 0)
