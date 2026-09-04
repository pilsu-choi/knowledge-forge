"""`eval_run`의 프로버넌스 - "이 숫자를 만든 설정과 코퍼스가 무엇이었나".

세 가지를 고정한다:

1. **config_hash가 랭킹 노브에 반응한다.** 이전에는 CLI 인자만 해싱해서
   `KF_VECTOR_MIN_SCORE`를 바꿔 돌린 스윕 11회가 전부 같은 해시(`54d9a917f28c`)로
   쌓였고, 나중에 설정을 복원할 방법이 없었다.
2. **코퍼스 불일치가 조용히 지나가지 않는다.** 어휘 arm은 1891 청크를, 벡터 arm은
   936개만 보던 상태에서 잰 점수가 "코드 변경의 결과"로 읽혔던 사고(2026-09-04).
3. **`--compare`가 비교 가능성을 말한다.** 다른 코퍼스/다른 설정/다른 파이프라인의
   두 run을 아무 말 없이 페어드 비교해 주던 문제.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from knowledge_forge.evaluation.golden import GoldenExpected, GoldenQuery, GoldenSet, IndexSnapshot
from knowledge_forge.evaluation.ir_compare import compare_runs
from knowledge_forge.evaluation.ir_runner import (
    NON_RANKING_SETTING_FIELDS,
    RANKING_SETTING_FIELDS,
    config_hash,
    embedding_consistency,
    index_warnings,
    ranking_settings,
    run_ir_eval,
    settings_diff,
    snapshot_counts,
    snapshot_drift,
)
from knowledge_forge.storage.eval_store import EvalRun, EvalRunQuery

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
MODEL = "openai/text-embedding-3-small"


def _snapshot(*, documents=358, chunks=2856, knowledge_objects=5107, embedded=2856, model=MODEL, kind="chunk") -> dict:
    by_model = {model: {kind: embedded}} if embedded else {}
    return {
        "documents": documents,
        "chunks": chunks,
        "knowledge_objects": knowledge_objects,
        "embeddings_by_kind": {kind: embedded} if embedded else {},
        "embeddings_by_model_and_kind": by_model,
    }


# --- (1) config_hash가 랭킹 노브를 담는다 -------------------------------


class _Knobs:
    """`Settings`의 최소 대역(주입용) - 실제 프로세스 설정에 의존하지 않는다."""

    def __init__(self, **overrides: Any):
        for name in RANKING_SETTING_FIELDS:
            setattr(self, name, None)
        self.vector_min_score = 0.45
        self.term_coverage_ratio = 2 / 3
        self.term_coverage_min = 2
        self.term_rare_df_max = 0
        self.latest_relevance_pool = 5
        self.embedding_model = MODEL
        for k, v in overrides.items():
            setattr(self, k, v)


def test_ranking_settings_reads_exactly_the_whitelist():
    knobs = ranking_settings(_Knobs())
    assert set(knobs) == set(RANKING_SETTING_FIELDS)
    assert knobs["vector_min_score"] == 0.45
    assert knobs["term_rare_df_max"] == 0


def test_no_credential_is_ever_recorded():
    """화이트리스트를 쓰는 이유의 절반 - `Settings`를 통째로 넣으면 API 키가
    eval_run에 평문으로 쌓인다."""
    secretish = re.compile(r"key|token|secret|password|credential|dsn|url", re.IGNORECASE)
    leaked = [f for f in RANKING_SETTING_FIELDS if secretish.search(f) and f != "embedding_base_url"]
    assert leaked == []
    from knowledge_forge.config import settings

    assert "embedding_api_key" not in ranking_settings(settings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("vector_min_score", 0.42),
        ("term_coverage_ratio", 0.5),
        ("term_coverage_min", 3),
        ("term_rare_df_max", 4),
        ("latest_relevance_pool", 7),
        ("embedding_model", "qwen/qwen3-embedding-8b"),
    ],
)
def test_changing_one_knob_changes_config_hash(field, value):
    """이 작업의 핵심 검증. 노브 하나만 움직여도 해시가 달라져야 한다."""
    base = {"limit": 10, "settings": ranking_settings(_Knobs())}
    moved = {"limit": 10, "settings": ranking_settings(_Knobs(**{field: value}))}
    assert config_hash(base) != config_hash(moved)
    # 그리고 "무엇이 달랐는지"까지 복원된다 - 해시만으로는 안 되는 부분.
    assert settings_diff(base, moved) == {f"settings.{field}": [getattr(_Knobs(), field), value]}


def test_config_hash_is_still_stable_for_the_same_settings():
    a = {"limit": 10, "settings": ranking_settings(_Knobs())}
    b = {"limit": 10, "settings": ranking_settings(_Knobs())}
    assert config_hash(a) == config_hash(b) and len(config_hash(a)) == 12


def test_every_setting_search_reads_is_classified():
    """가드: `search/` 어딘가에서 `settings.<이름>`을 새로 읽기 시작했는데 화이트
    리스트(또는 명시적 제외 목록)에 없으면 여기서 실패한다. 이게 없으면 다음 노브가
    추가되는 순간 정확히 같은 버그가 조용히 재발한다."""
    root = Path(__file__).resolve().parents[2] / "src" / "knowledge_forge" / "search"
    referenced = {
        name
        for path in root.rglob("*.py")
        for name in re.findall(r"\bsettings\.([a-z_][a-z0-9_]*)", path.read_text(encoding="utf-8"))
    }
    assert referenced, "search/ 아래에서 settings 참조를 하나도 못 찾았다 - 스캐너가 깨진 것"
    unclassified = referenced - set(RANKING_SETTING_FIELDS) - NON_RANKING_SETTING_FIELDS
    assert unclassified == set(), (
        f"랭킹에 쓰이는데 config_hash에 안 들어가는 설정: {sorted(unclassified)}. "
        "ir_runner.py의 RANKING_SETTING_FIELDS(또는 NON_RANKING_SETTING_FIELDS)에 추가하라."
    )


# --- (2) 코퍼스/임베딩 정합 --------------------------------------------


def test_consistent_index_is_ok():
    c = embedding_consistency(_snapshot(), MODEL)
    assert c["status"] == "ok" and c["consistent"] is True
    assert index_warnings(_snapshot(), c) == []


def test_partial_backfill_is_the_incident_and_is_flagged():
    """실제 사고 그대로: 청크 1891개, 임베딩 936개."""
    snap = _snapshot(documents=326, chunks=1891, embedded=936)
    c = embedding_consistency(snap, MODEL)
    assert c["status"] == "mismatch" and c["consistent"] is False
    assert c["grains"]["chunk"] == {"total": 1891, "embedded": 936, "coverage": pytest.approx(936 / 1891)}
    (warning,) = index_warnings(snap, c)
    assert "936/1891" in warning and "벡터 arm" in warning


def test_embeddings_under_a_different_model_count_as_missing():
    """모델 이름이 한 글자만 달라도 벡터 arm은 0행이다 - grain 합계만 보면
    '2856/2856 정합'으로 보이는 상황."""
    snap = _snapshot(model="text-embedding-3-small")  # KF_EMBEDDING_MODEL은 openai/ 접두
    c = embedding_consistency(snap, MODEL)
    assert c["status"] == "missing"
    (warning,) = index_warnings(snap, c)
    assert "text-embedding-3-small=2856" in warning


def test_old_snapshot_without_model_breakdown_is_unknown_not_broken():
    old = {"documents": 263, "chunks": 936, "knowledge_objects": 100, "embeddings_by_kind": {"chunk": 936}}
    c = embedding_consistency(old, MODEL)
    assert c["status"] == "unknown" and c["consistent"] is True
    assert index_warnings(old, c) == []


def test_knowledge_object_grain_is_not_required_when_nothing_embedded_it():
    """`KF_EMBEDDING_SOURCE=chunk` 코퍼스에서 KO 임베딩 0은 결함이 아니라 설정이다."""
    c = embedding_consistency(_snapshot(knowledge_objects=5107), MODEL)
    assert c["status"] == "ok"
    assert c["grains"]["knowledge_object"] == {"total": 5107, "embedded": 0, "coverage": 0.0}


def test_snapshot_drift_and_counts():
    before, after = _snapshot(documents=263, chunks=936, embedded=936), _snapshot(documents=326, chunks=1891, embedded=936)
    drift = snapshot_drift(before, after)
    assert drift["documents"] == [263, 326] and drift["chunks"] == [936, 1891]
    assert "embeddings_by_kind" not in drift  # 936으로 그대로 - 그래서 불일치가 생겼다
    # 새 키(warnings/finished/...)는 코퍼스 비교에 끼어들지 않는다.
    assert snapshot_counts({**before, "warnings": ["x"], "finished": after}) == snapshot_counts(before)


# --- (2b) 러너가 실제로 두 번 찍고 경고한다 -----------------------------


class FakeEvalStore:
    """`run_ir_eval`이 부르는 부분만. `snapshots`를 순서대로 돌려줘 run 도중의
    코퍼스 변화를 흉내 낸다."""

    def __init__(self, snapshots: list[dict]):
        self._snapshots = list(snapshots)
        self.saved: list[EvalRun] = []
        self.snapshot_calls = 0

    async def snapshot_index(self, project_id=None) -> dict:
        self.snapshot_calls += 1
        return self._snapshots[min(self.snapshot_calls - 1, len(self._snapshots) - 1)]

    async def content_hashes_for(self, document_ids) -> dict[str, str]:
        return {}

    async def save_eval_run(self, run: EvalRun) -> EvalRun:
        self.saved.append(run.model_copy(deep=True))
        return run

    async def save_eval_run_queries(self, run_id: str, rows) -> int:
        return len(rows)


class _Retriever:
    async def __call__(self, query: str, **kwargs: Any):
        from tests.evaluation.test_ir_runner import FakeResult

        return FakeResult(["A"], [1.0])


def _golden() -> GoldenSet:
    return GoldenSet(
        golden_version="vtest",
        created_at=NOW,
        index_snapshot=IndexSnapshot(documents=1, chunks=1, knowledge_objects=1),
        queries=[
            GoldenQuery(
                id="q1",
                query="what is A?",
                primary_type="factoid",
                types=["factoid"],
                expected=GoldenExpected(document_ids=["A"]),
            )
        ],
    )


async def _run(snapshots: list[dict], **kwargs) -> tuple[EvalRun, FakeEvalStore]:
    store = FakeEvalStore(snapshots)
    run = await run_ir_eval(
        _golden(),
        store=object(),  # type: ignore[arg-type]
        eval_store=store,  # type: ignore[arg-type]
        retrieve_fn=_Retriever(),
        settings_snapshot=ranking_settings(_Knobs(**kwargs)),
    )
    return run, store


async def test_runner_snapshots_start_and_finish_and_flags_drift(caplog):
    caplog.set_level(logging.WARNING, logger="knowledge_forge.evaluation.ir_runner")
    before = _snapshot(documents=263, chunks=936, embedded=936)
    after = _snapshot(documents=326, chunks=1891, embedded=936)
    run, store = await _run([before, after])

    assert store.snapshot_calls == 2
    # 상위 키는 시작 시점 그대로(하위호환), 종료 스냅샷은 그 옆에.
    assert snapshot_counts(run.index_snapshot) == snapshot_counts(before)
    assert run.index_snapshot["finished"] == after
    assert run.index_snapshot["changed_during_run"] is True
    assert run.index_snapshot["drift"]["chunks"] == [936, 1891]
    assert any("코퍼스가 변했다" in w for w in run.index_snapshot["warnings"])
    assert run.summary["index_warnings"] == run.index_snapshot["warnings"]
    assert "코퍼스가 변했다" in caplog.text


async def test_runner_warns_loudly_at_start_on_embedding_mismatch(caplog):
    caplog.set_level(logging.WARNING, logger="knowledge_forge.evaluation.ir_runner")
    snap = _snapshot(documents=326, chunks=1891, embedded=936)
    run, store = await _run([snap])

    assert run.index_snapshot["embedding_coverage"]["status"] == "mismatch"
    assert any("936/1891" in w for w in run.index_snapshot["warnings"])
    assert "936/1891" in caplog.text
    # run 시작 시점에(=첫 저장 전에) 이미 경고가 붙어 있어야 한다 - 사람이 멈출 수 있는 지점.
    assert store.saved[0].index_snapshot["warnings"]


async def test_runner_records_the_knobs_in_config_and_hash():
    run, _ = await _run([_snapshot()], vector_min_score=0.42)
    other, _ = await _run([_snapshot()], vector_min_score=0.50)
    assert run.config["settings"]["vector_min_score"] == 0.42
    assert run.config_hash != other.config_hash
    assert run.index_snapshot["changed_during_run"] is False


async def test_clean_run_has_no_index_warnings():
    run, _ = await _run([_snapshot()])
    assert run.index_snapshot["warnings"] == []
    assert run.summary["index_warnings"] == []


# --- (3) --compare가 비교 가능성을 검증한다 -----------------------------


def _eval_run(run_id: str, *, snapshot: dict | None = None, config: dict | None = None, cfg_hash: str = "cfg") -> EvalRun:
    return EvalRun(
        run_id=run_id,
        golden_version="v0",
        golden_path="tests/eval/golden/v0.json",
        config=config if config is not None else {"limit": 10, "settings": ranking_settings(_Knobs())},
        config_hash=cfg_hash,
        index_snapshot=snapshot if snapshot is not None else _snapshot(),
        started_at=NOW,
        finished_at=NOW,
    )


def _rows(run_id: str, metrics: dict[str, float]) -> list[EvalRunQuery]:
    return [
        EvalRunQuery(run_id=run_id, query_id="q1", query_text="q1", primary_type="factoid", types=["factoid"], metrics=metrics)
    ]


def test_compare_warns_on_different_corpora():
    a = _eval_run("a", snapshot=_snapshot(documents=263, chunks=936, embedded=936))
    b = _eval_run("b", snapshot=_snapshot(), cfg_hash="cfg2")
    report = compare_runs(a, _rows("a", {"ndcg@10": 0.5}), b, _rows("b", {"ndcg@10": 0.6}))
    assert report["snapshot_mismatch"] is True
    assert any("서로 다른 코퍼스" in w and "263" in w for w in report["warnings"])


def test_compare_names_the_setting_that_differs():
    a = _eval_run("a")
    b = _eval_run("b", config={"limit": 10, "settings": ranking_settings(_Knobs(vector_min_score=0.42))}, cfg_hash="cfg2")
    report = compare_runs(a, _rows("a", {"ndcg@10": 0.5}), b, _rows("b", {"ndcg@10": 0.6}))
    assert report["config_diff"] == {"settings.vector_min_score": [0.45, 0.42]}
    assert any("vector_min_score" in w for w in report["warnings"])


def test_compare_warns_when_only_one_run_used_a_strategies_override():
    """override는 후보 생성뿐 아니라 후처리까지 갈아치우므로 '파이프라인 둘'의 비교다."""
    a = _eval_run("a")
    b = _eval_run(
        "b",
        config={"limit": 10, "strategies_override": ["vector"], "settings": ranking_settings(_Knobs())},
        cfg_hash="cfg2",
    )
    report = compare_runs(
        a, _rows("a", {"ndcg@10": 0.5, "count_match": 1.0}), b, _rows("b", {"ndcg@10": 0.6})
    )
    assert any("파이프라인 둘" in w for w in report["warnings"])
    assert any("count_match" in w for w in report["warnings"])  # 한쪽에만 있는 지표


def test_compare_surfaces_each_runs_index_health():
    a = _eval_run(
        "a",
        snapshot={
            **_snapshot(chunks=1891, embedded=936),
            "embedding_coverage": embedding_consistency(_snapshot(chunks=1891, embedded=936), MODEL),
            "changed_during_run": True,
            "drift": {"chunks": [936, 1891]},
        },
    )
    b = _eval_run("b", cfg_hash="cfg2")
    report = compare_runs(a, _rows("a", {"ndcg@10": 0.5}), b, _rows("b", {"ndcg@10": 0.6}))
    assert any("run A는 측정 도중 코퍼스가 변했다" in w for w in report["warnings"])
    assert any("run A는 임베딩/코퍼스가 어긋난" in w for w in report["warnings"])


def test_compare_warns_on_different_golden_files_with_the_same_version():
    a = _eval_run("a")
    b = _eval_run("b", cfg_hash="cfg2").model_copy(update={"golden_path": "/tmp/v0-edited.json"})
    report = compare_runs(a, _rows("a", {"ndcg@10": 0.5}), b, _rows("b", {"ndcg@10": 0.6}))
    assert any("골든셋 파일 경로가 다르다" in w for w in report["warnings"])


def test_compare_of_two_identical_setups_stays_quiet_except_the_known_note():
    a, b = _eval_run("a"), _eval_run("b")
    report = compare_runs(a, _rows("a", {"ndcg@10": 0.5}), b, _rows("b", {"ndcg@10": 0.6}))
    assert report["warnings"] == [
        "both runs share config_hash - the delta measures run-to-run noise, not a config change"
    ]
