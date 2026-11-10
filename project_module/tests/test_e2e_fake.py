"""End to end on generated streams with a rule-following fake model and the real cost ledger."""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from fakes import FakeTransport, text_reply, tool_reply
from project_module.config import ArmName, Budgets, ExperimentConfig, load_models
from project_module.contracts import ChatResult
from project_module.evaluation.load import load_run
from project_module.evaluation.report import analyze_runs, load_streams, run_gate
from project_module.evaluation.summary import summarize_run
from project_module.llm.ledger import CostLedger
from project_module.llm.metered import MeteredClient
from project_module.runner import generate_streams, incomplete_jobs, run_experiment

MODELS = Path(__file__).resolve().parents[2] / "configs" / "models.yaml"
ALL_ARMS: list[ArmName] = list(get_args(ArmName))
SESSIONS = 2
QUESTIONS = 3
STREAMS = ["e2e-01", "e2e-02"]


class PolicyTransport(FakeTransport):
    """Behaves like a cooperative model: uses each tool it is offered once, then finishes.

    Online questions are answered with the gold SQL of the question, so every arm should be
    graded correct; consolidation writes `injected.md` once; probes answer at once.
    """

    def __init__(self, gold_by_question: dict[str, str]) -> None:
        super().__init__()
        self._gold = gold_by_question

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str,
        max_completion_tokens: int,
    ) -> ChatResult:
        self.calls.append({"model": model, "tools": tools, "reasoning_effort": reasoning_effort})
        names = {tool["function"]["name"] for tool in tools or []}
        step = sum(message["role"] == "assistant" for message in messages)
        if "answer" in names:
            return tool_reply(("answer", {"answer": "orders"}))
        if "write_file" in names:
            if step == 0:
                return tool_reply(
                    ("write_file", {"path": "memory/injected.md", "content": "Check units.\n"})
                )
            return text_reply("Updated the memory.")
        if "submit_sql" in names:
            plan: list[tuple[str, dict[str, Any]]] = []
            if "search_transcripts" in names:
                plan.append(("search_transcripts", {"query": "orders table"}))
            if "memory_read" in names:
                plan.append(("memory_read", {"path": "injected.md"}))
            plan.append(("query", {"sql": "SELECT name FROM sqlite_master"}))
            if step < len(plan):
                return tool_reply(plan[step])
            user = next(m["content"] for m in messages if m["role"] == "user")
            return tool_reply(("submit_sql", {"sql": self._gold[user]}))
        raise AssertionError(f"unexpected tool set {sorted(names)}")


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        name="e2e",
        phase="test",
        arms=ALL_ARMS,
        stream_ids=STREAMS,
        # Seeds whose streams share no question text, so the fake can look up the gold SQL.
        seeds=[20, 21],
        sessions=SESSIONS,
        questions_per_session=QUESTIONS,
        inject_false_notes=False,
        agent_model="gpt-6-luna",
        consolidator_model="gpt-6-luna",
        embedding_model="text-embedding-3-large",
        probe_sessions=[SESSIONS],
        probes_per_checkpoint=2,
        concurrency=3,
        budgets=Budgets(phase_cap_usd=1.0),
    )


@pytest.fixture
def finished_run(tmp_path: Path) -> tuple[ExperimentConfig, Path, Path, Path]:
    config = _config()
    streams_root = tmp_path / "streams"
    generate_streams(config, streams_root)
    gold: dict[str, str] = {}
    for stream in load_streams(config, streams_root).values():
        for question in stream.questions:
            sql = question.gold_sql_by_epoch[stream.epoch_of(question.session)]
            assert gold.setdefault(question.text, sql) == sql, "ambiguous question text"
    ledger_file = tmp_path / "ledger.sqlite"
    ledger = CostLedger(ledger_file, phase="test", phase_cap_usd=1.0, hard_cap_usd=200.0)
    client = MeteredClient(PolicyTransport(gold), ledger, load_models(MODELS))
    run_dir = run_experiment(
        config,
        out_root=tmp_path / "outputs",
        streams_root=streams_root,
        clbench_root=tmp_path / "no-clbench",
        client=client,
    )
    return config, run_dir, ledger_file, streams_root


def test_e2e_fake_all_arms_two_sessions(
    finished_run: tuple[ExperimentConfig, Path, Path, Path],
) -> None:
    config, run_dir, ledger_file, streams_root = finished_run
    assert incomplete_jobs(config, run_dir) == {}

    data = load_run(run_dir, ledger_file)
    assert len(data.results) == len(STREAMS) * len(ALL_ARMS) * SESSIONS * QUESTIONS
    assert all(result.correct for result in data.results)
    assert {(r.arm, r.ok_queries) for r in data.results} == {(arm, 1) for arm in ALL_ARMS}
    # Probes run at the checkpoint for every arm that has memory to probe.
    assert {p.arm for p in data.probes} == set(ALL_ARMS) - {"A0"}
    assert {p.session for p in data.probes} == {SESSIONS}
    # Memory arms consolidate after every session but the last.
    assert sorted((c.stream_id, c.arm, c.session) for c in data.consolidations) == [
        (stream_id, arm, 1) for stream_id in STREAMS for arm in ("A1", "A3", "A4")
    ]
    # Every call was booked under this run, including A2's index embeddings.
    components = {(row["arm"], row["component"]) for row in data.costs}
    assert ("A2", "embedding") in components
    assert {"online", "consolidation", "probe"} <= {component for _, component in components}

    summary = summarize_run(data, arms=ALL_ARMS, streams=load_streams(config, streams_root))
    assert summary["streams"]["analyzed"] == STREAMS
    assert summary["primary"] is not None
    assert summary["primary"]["n"] == len(STREAMS)
    assert summary["primary"]["mean"] == 0.0


def test_e2e_analysis_writes_metrics_figures_and_gate(
    finished_run: tuple[ExperimentConfig, Path, Path, Path], tmp_path: Path
) -> None:
    _, run_dir, ledger_file, streams_root = finished_run
    out = tmp_path / "analysis"

    analyze_runs(
        [run_dir], out=out, ledger=ledger_file, streams_root=streams_root, mechanical_only=False
    )
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics[run_dir.name]["primary"]["treatment"] == "A4"
    assert (out / f"{run_dir.name}_session_accuracy.png").is_file()
    assert (out / f"{run_dir.name}_pareto.png").is_file()

    mechanical = tmp_path / "mechanical"
    analyze_runs(
        [run_dir],
        out=mechanical,
        ledger=ledger_file,
        streams_root=streams_root,
        mechanical_only=True,
    )
    assert "primary" not in json.loads((mechanical / "metrics.json").read_text())[run_dir.name]
    assert not list(mechanical.glob("*.png"))

    # No false notes were injected, so the mechanism check cannot pass.
    result = run_gate(run_dir, run_dir, out=out, ledger=ledger_file, streams_root=streams_root)
    assert json.loads((out / "gate.json").read_text())["passed"] is False
    assert result.required["evidence_present"] is False
    assert result.required["generator_ok"] is True
