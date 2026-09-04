"""_build_collector() is the seam extract/schedule use to pick a Collector
via --source (wiki/roadmap.md 1단계) - "local" (default) needs PATH and
returns a LocalFileCollector, anything else delegates to
collectors/saas/factory.py's build_*_collector() and ignores PATH.
"""

from datetime import timezone

import pytest
import typer

from knowledge_forge import cli
from knowledge_forge.collectors.local_file import LocalFileCollector


def test_local_source_requires_path():
    with pytest.raises(typer.Exit):
        cli._build_collector("local", None)


def test_local_source_returns_local_file_collector(tmp_path):
    target = tmp_path / "doc.md"
    target.write_text("hello")

    collector = cli._build_collector("local", target)

    assert isinstance(collector, LocalFileCollector)


def test_unknown_source_exits():
    with pytest.raises(typer.Exit):
        cli._build_collector("carrier-pigeon", None)


def test_saas_source_delegates_to_matching_factory_builder(monkeypatch):
    sentinel = object()
    monkeypatch.setitem(cli.SAAS_COLLECTOR_BUILDERS, "slack", lambda: sentinel)

    assert cli._build_collector("slack", None) is sentinel


# _parse_datetime_arg() is `backfill`'s --since/--until parser (wiki/scheduling.md
# "놓친 구간 재수집") - naive input is treated as UTC (see collectors/base.py's
# ensure_utc) since every RawItem.modified_at it's compared against is UTC-aware.


def test_parse_datetime_arg_accepts_date_only_and_treats_it_as_utc_midnight():
    parsed = cli._parse_datetime_arg("2026-08-01", "--since")

    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert (parsed.year, parsed.month, parsed.day, parsed.hour) == (2026, 8, 1, 0)


def test_parse_datetime_arg_accepts_naive_datetime_and_normalizes_to_utc():
    parsed = cli._parse_datetime_arg("2026-08-01T09:30:00", "--since")

    assert parsed == parsed.replace(tzinfo=timezone.utc)
    assert parsed.hour == 9 and parsed.minute == 30


def test_parse_datetime_arg_preserves_explicit_offset():
    parsed = cli._parse_datetime_arg("2026-08-01T09:00:00+09:00", "--since")

    assert parsed.utcoffset().total_seconds() == 9 * 3600


def test_parse_datetime_arg_rejects_unparseable_value():
    with pytest.raises(typer.Exit):
        cli._parse_datetime_arg("not-a-date", "--since")


# --- eval-ir ---------------------------------------------------------------
# Smoke test through typer's CliRunner with the retriever injected via the
# runner's lazy `default_retrieve_fn` seam (search/retrieve.py may not
# exist yet) and PipelineStore replaced so no Postgres is needed.


class _FakeResult:
    def __init__(self, document_ids, predicted_count=None):
        self.document_ids = document_ids
        self.scores = [1.0 / (i + 1) for i in range(len(document_ids))]
        self.predicted_count = predicted_count
        self.strategies = ["fake"]
        self.steps = [{"strategy": "fake", "source": "chunk", "n_hits": len(document_ids), "detail": {}}]
        self.elapsed_ms = 1.0


class _FakePipelineStore:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


def _install_fake_retriever(monkeypatch, golden_path="tests/eval/golden/v0.json"):
    """A retriever that answers every golden query perfectly, so the smoke
    test can assert exact metric values rather than just 'it ran'."""
    from knowledge_forge.evaluation import ir_runner
    from knowledge_forge.evaluation.golden import load_golden

    golden = load_golden(golden_path)
    by_text = {q.query: q for q in golden.queries}

    async def _retrieve(query, **kwargs):
        q = by_text[query]
        return _FakeResult(list(q.expected.document_ids), predicted_count=q.expected.count)

    monkeypatch.setattr(ir_runner, "default_retrieve_fn", lambda: _retrieve)
    monkeypatch.setattr(cli, "PipelineStore", _FakePipelineStore)
    monkeypatch.setattr(cli.settings, "database_url", "postgresql://unused")
    return golden


def test_eval_ir_requires_database_url(monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setattr(cli.settings, "database_url", None)
    result = CliRunner().invoke(cli.app, ["eval-ir", "--golden", "tests/eval/golden/v0.json", "--no-store"])
    assert result.exit_code == 1
    assert "KF_DATABASE_URL" in result.output


def test_eval_ir_requires_golden_or_mode(monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setattr(cli.settings, "database_url", "postgresql://unused")
    result = CliRunner().invoke(cli.app, ["eval-ir", "--no-store"])
    assert result.exit_code == 1
    assert "--golden" in result.output


def test_eval_ir_no_store_json_smoke(monkeypatch, tmp_path):
    import json

    from typer.testing import CliRunner

    golden = _install_fake_retriever(monkeypatch)
    out = tmp_path / "run.json"
    ids = ["v0-factoid-001", "v0-null-001", "v0-aggregation-001", "v0-set_query-001"]
    args = ["eval-ir", "--golden", "tests/eval/golden/v0.json", "--no-store", "--no-llm", "--json", str(out)]
    for qid in ids:
        args += ["--query-id", qid]

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert "overall: n=4" in result.output
    assert "run_id=" in result.output and "--no-store" in result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["golden_version"] == golden.golden_version
    assert payload["config"]["use_llm_constraints"] is False
    assert payload["config"]["provider"] is None
    assert payload["summary"]["overall"]["n"] == 4
    assert payload["summary"]["errors"] == []
    assert payload["summary"]["staleness_checked"] is False
    assert sorted(q["query_id"] for q in payload["queries"]) == sorted(ids)
    by_id = {q["query_id"]: q for q in payload["queries"]}
    assert by_id["v0-factoid-001"]["metrics"]["hit@1"] == 1.0
    assert by_id["v0-null-001"]["metrics"] == {"none_correct": 1.0}
    assert by_id["v0-aggregation-001"]["metrics"]["count_match"] == 1.0
    assert by_id["v0-set_query-001"]["metrics"]["exhaustive_recall"] == 1.0
    assert by_id["v0-set_query-001"]["strategies"] == ["fake"]


def test_eval_ir_type_filter_and_custom_k(monkeypatch, tmp_path):
    import json

    from typer.testing import CliRunner

    _install_fake_retriever(monkeypatch)
    out = tmp_path / "run.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "eval-ir", "--golden", "tests/eval/golden/v0.json", "--no-store", "--no-llm",
            "--type", "negative,null", "--k", "1,2", "--json", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["config"]["k_values"] == [1, 2]
    assert {q["primary_type"] for q in payload["queries"]} == {"negative", "null"}
    assert payload["summary"]["overall"]["none_correct"] == 1.0


def test_eval_ir_rejects_bad_k(monkeypatch):
    from typer.testing import CliRunner

    _install_fake_retriever(monkeypatch)
    result = CliRunner().invoke(
        cli.app, ["eval-ir", "--golden", "tests/eval/golden/v0.json", "--no-store", "--no-llm", "--k", "1,x"]
    )
    assert result.exit_code == 1
    assert "--k" in result.output


def test_format_ir_summary_lists_errors_and_stale():
    summary = {
        "overall": {"n": 2, "mrr": 0.5, "ndcg@10": 0.4},
        "by_type": {"factoid": {"n": 1, "mrr": 1.0, "ndcg@10": 0.8}, "null": {"n": 1, "none_correct": 0.0}},
        "errors": [{"query_id": "q-x", "error": "RuntimeError: boom"}],
        "stale_queries": [{"query_id": "q-y", "reason": "changed", "document_ids": ["d1"]}],
        "staleness_checked": True,
    }
    text = cli._format_ir_summary(summary)
    assert text.startswith("overall: n=2 mrr=0.500 ndcg@10=0.400")
    assert "factoid" in text and "none_correct" in text
    assert "q-x: RuntimeError: boom" in text
    assert "q-y: changed d1" in text
    # hit@1 / count_match are absent everywhere, so they are not columns.
    assert "hit@1" not in text and "count_match" not in text


# --- eval-gen --------------------------------------------------------------
# 같은 방식의 스모크 테스트 - EvalStore/PipelineStore/ChatProvider를 전부 가짜로
# 바꿔 Postgres도 LLM도 없이 CLI 배선(draft 강제, 파일 저장, 통계 출력)만 본다.


class _FakeEvalStore:
    """`eval-gen`이 EvalStore에 요구하는 세 메서드만 흉내낸다."""

    candidates: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def list_chunk_candidates(self, **kwargs):
        self.last_kwargs = kwargs
        _FakeEvalStore.last_kwargs = kwargs
        return list(_FakeEvalStore.candidates)

    async def snapshot_index(self, project_id=None):
        return {"documents": 3, "chunks": 9, "knowledge_objects": 5, "embeddings_by_kind": {}}

    async def sources_by_type(self, project_id=None):
        return {"slack": 2, "gmail": 1}


def _install_fake_gen(monkeypatch, responses=None):
    import json as _json

    from knowledge_forge.chat_provider import AssistantTurn, ChatProvider
    from knowledge_forge.evaluation import gen
    from knowledge_forge.storage.eval_store import ChunkSample

    class _Provider(ChatProvider):
        async def send(self, messages, tools, system):
            text = responses if responses is not None else _json.dumps(
                {
                    "queries": [
                        {
                            "query": "NH MVP 프로젝트의 인터뷰 예상 장소는 어디인가?",
                            "primary_type": "factoid",
                            "types": ["factoid", "entity_centric"],
                            "answer": "농협타워 14층",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            return AssistantTurn(text=text, tool_calls=[], stop_reason="end_turn")

    _FakeEvalStore.candidates = [
        ChunkSample(
            chunk_id="c1",
            document_id="d1",
            source_id="s1",
            source_type="slack",
            content_hash="h1",
            text="NH MVP 3차 인터뷰는 농협타워 14층 화상회의실1에서 진행될 예정이다." * 3,
            token_count=120,
            title="NH MVP 인터뷰 안내",
            uri="slack://c/1",
        )
    ]

    async def _retrieve(query, **kwargs):
        return _FakeResult(["d1"])

    monkeypatch.setattr(gen, "default_retrieve_fn", lambda: _retrieve)
    monkeypatch.setattr(cli, "EvalStore", _FakeEvalStore)
    monkeypatch.setattr(cli, "PipelineStore", _FakePipelineStore)
    monkeypatch.setattr(cli, "get_chat_provider", lambda **kwargs: _Provider())
    monkeypatch.setattr(cli.settings, "database_url", "postgresql://unused")


def test_eval_gen_requires_database_url(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    monkeypatch.setattr(cli.settings, "database_url", None)
    result = CliRunner().invoke(cli.app, ["eval-gen", "--out", str(tmp_path / "v1-draft.json")])
    assert result.exit_code == 1
    assert "KF_DATABASE_URL" in result.output


def test_eval_gen_refuses_non_draft_version_and_filename(monkeypatch, tmp_path):
    """사람 검수를 거치지 않은 결과가 정식 버전 파일(v0.json/v1.json)로 위장하는
    걸 구조적으로 막는다 - 골든셋 버전 파일은 불변이 원칙이라 한 번 섞이면
    과거 eval run과의 비교가 조용히 깨진다."""
    from typer.testing import CliRunner

    _install_fake_gen(monkeypatch)

    bad_version = CliRunner().invoke(
        cli.app, ["eval-gen", "--golden-version", "v1", "--out", str(tmp_path / "v1-draft.json")]
    )
    assert bad_version.exit_code == 1 and "draft" in bad_version.output

    bad_name = CliRunner().invoke(cli.app, ["eval-gen", "--out", str(tmp_path / "v1.json")])
    assert bad_name.exit_code == 1 and "draft" in bad_name.output


def test_eval_gen_refuses_to_overwrite_without_force(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    _install_fake_gen(monkeypatch)
    out = tmp_path / "v1-draft.json"
    out.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["eval-gen", "--out", str(out)])

    assert result.exit_code == 1 and "--force" in result.output


def test_eval_gen_writes_loadable_draft_and_stats(monkeypatch, tmp_path):
    """CLI가 실제로 `load_golden`이 읽는 파일을 쓰고, 통계/검수 안내를 출력하는지."""
    import json

    from typer.testing import CliRunner

    from knowledge_forge.evaluation.golden import load_golden

    _install_fake_gen(monkeypatch)
    out = tmp_path / "v1-draft.json"
    stats_out = tmp_path / "stats.json"

    result = CliRunner().invoke(
        cli.app,
        ["eval-gen", "--out", str(out), "--chunks", "1", "--per-chunk", "1", "--stats-json", str(stats_out)],
    )

    assert result.exit_code == 0, result.output
    golden = load_golden(out)
    assert golden.golden_version == "v1-draft"
    assert golden.index_snapshot.sources_by_type == {"slack": 2, "gmail": 1}
    assert [q.origin for q in golden.queries] == ["synthetic"]
    assert golden.queries[0].expected.document_ids == ["d1"]
    assert "rank=1" in golden.queries[0].notes  # 자동 검증이 실제로 돌았다
    stats = json.loads(stats_out.read_text(encoding="utf-8"))
    assert stats["accepted"] == 1 and stats["verified"] == 1 and stats["flagged_hard"] == 0
    assert "사람 검수 필요" in result.output


def test_eval_gen_passes_noise_filters_to_the_store(monkeypatch, tmp_path):
    """`--min-tokens`/`--source-type`은 후보 SQL로 그대로 내려가야 한다 - 짧은
    청크(목차/서명)에서는 '이 청크로만 답할 수 있는 질문'이 나오지 않는다."""
    from typer.testing import CliRunner

    _install_fake_gen(monkeypatch)

    result = CliRunner().invoke(
        cli.app,
        [
            "eval-gen", "--out", str(tmp_path / "v1-draft.json"), "--min-tokens", "150",
            "--source-type", "slack,gmail", "--no-verify",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeEvalStore.last_kwargs["min_token_count"] == 150
    assert _FakeEvalStore.last_kwargs["source_types"] == ["slack", "gmail"]


def test_eval_gen_exits_when_everything_is_filtered(monkeypatch, tmp_path):
    """0개가 나왔는데 빈 파일을 쓰면 다음 `eval-ir`가 로더 에러로 죽는다 -
    통계만 출력하고 파일은 만들지 않는다."""
    import json

    from typer.testing import CliRunner

    _install_fake_gen(
        monkeypatch,
        responses=json.dumps({"queries": [{"query": "짧음?", "primary_type": "factoid", "types": ["factoid"]}]}),
    )
    out = tmp_path / "v1-draft.json"

    result = CliRunner().invoke(cli.app, ["eval-gen", "--out", str(out), "--no-verify"])

    assert result.exit_code == 1
    assert not out.exists()
    assert "too_short" in result.output
