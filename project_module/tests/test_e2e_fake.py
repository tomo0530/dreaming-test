"""End to end on generated streams with a rule-following fake model and the real cost ledger."""

import json
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

import pytest
from fakes import FakeTransport, text_reply, tool_reply
from project_module.config import (
    ArmName,
    Budgets,
    ExperimentConfig,
    load_experiment,
    load_models,
)
from project_module.contracts import ChatResult, ToolChoice
from project_module.evaluation.followup_gate import (
    count_main_jobs,
    evaluate_followup_gate,
    stream_validation_problems,
)
from project_module.evaluation.load import load_run
from project_module.evaluation.report import analyze_runs, load_streams, run_gate
from project_module.evaluation.summary import summarize_run
from project_module.llm.ledger import CostLedger
from project_module.llm.metered import MeteredClient
from project_module.manifest import load_manifest
from project_module.prompts import load_prompt
from project_module.runner import generate_streams, incomplete_jobs, run_experiment
from project_module.schemadrift.models import Stream

REPO = Path(__file__).resolve().parents[2]
MODELS = REPO / "configs" / "models.yaml"
FOLLOWUP = REPO / "configs" / "experiments" / "followup"
ALL_ARMS: list[ArmName] = list(get_args(ArmName))
SESSIONS = 2
QUESTIONS = 3
STREAMS = ["e2e-01", "e2e-02"]


class PolicyTransport(FakeTransport):
    """Behaves like a cooperative model: uses each tool it is offered once, then finishes.

    Online questions are answered with the gold SQL of the question, so every arm should be
    graded correct; the forced note turn writes the note it is asked for; consolidation reads
    the notes it finds, queries the database when it may, and writes `injected.md` once;
    probes answer at once.
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
        tool_choice: ToolChoice | None = None,
    ) -> ChatResult:
        self.calls.append(
            {
                "model": model,
                "tools": tools,
                "reasoning_effort": reasoning_effort,
                "messages": [dict(message) for message in messages],
                "tool_choice": tool_choice,
            }
        )
        names = {tool["function"]["name"] for tool in tools or []}
        step = sum(message["role"] == "assistant" for message in messages)
        if isinstance(tool_choice, dict) and tool_choice["name"] == "memory_write":
            return self._note(messages)
        if "answer" in names:
            return tool_reply(("answer", {"answer": "orders"}))
        if "write_file" in names:
            return self._consolidate(tools or [], messages, step)
        if "submit_sql" in names:
            plan: list[tuple[str, dict[str, Any]]] = []
            if "search_transcripts" in names:
                plan.append(("search_transcripts", {"query": "orders table"}))
            if "memory_read" in names:
                plan.append(("memory_read", {"path": "injected.md"}))
            plan.append(("query", {"sql": "SELECT name FROM sqlite_master"}))
            if step < len(plan):
                return tool_reply(plan[step])
            return tool_reply(("submit_sql", {"sql": self._gold_sql(messages)}))
        raise AssertionError(f"unexpected tool set {sorted(names)}")

    def _gold_sql(self, messages: list[dict[str, Any]]) -> str:
        # Arms may put retrieved context before the question text.
        user = next(m["content"] for m in messages if m["role"] == "user")
        return self._gold[max((text for text in self._gold if user.endswith(text)), key=len)]

    @staticmethod
    def _note(messages: list[dict[str, Any]]) -> ChatResult:
        path = re.search(r"path `([^`]+)`", messages[-1]["content"])
        assert path is not None, "the note prompt names its path"
        content = "- orders: row per order (unverified)\n"
        return tool_reply(("memory_write", {"path": path.group(1), "content": content}))

    @staticmethod
    def _consolidate(
        tools: list[dict[str, Any]], messages: list[dict[str, Any]], step: int
    ) -> ChatResult:
        names = {tool["function"]["name"] for tool in tools}
        read_file = next(t["function"] for t in tools if t["function"]["name"] == "read_file")
        plan: list[tuple[str, dict[str, Any]]] = []
        if "transcripts" in read_file["description"]:
            # The phases that may read transcripts also look at the notes the agent left.
            plan.append(("glob", {"pattern": "memory/notes/*.md"}))
            listing = next((m["content"] for m in messages if m["role"] == "tool"), "")
            notes = [line for line in listing.splitlines() if line.startswith("memory/notes/")]
            if notes:
                plan.append(("read_file", {"path": notes[0]}))
            if "query" in names:
                plan.append(("query", {"sql": "SELECT name FROM sqlite_master"}))
        plan.append(("write_file", {"path": "memory/injected.md", "content": "Check units.\n"}))
        if step < len(plan):
            return tool_reply(plan[step])
        return text_reply("Updated the memory.")


def _gold_by_question(streams: Iterable[Stream]) -> dict[str, str]:
    """Gold SQL by question text; the streams must not share a text with different gold."""
    gold: dict[str, str] = {}
    for stream in streams:
        for question in stream.questions:
            sql = question.gold_sql_by_epoch[stream.epoch_of(question.session)]
            assert gold.setdefault(question.text, sql) == sql, "ambiguous question text"
    return gold


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
    gold = _gold_by_question(load_streams(config, streams_root).values())
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


# --- follow-up study: the five pilot-shaped runs and their gate ------------------------------

PILOT_LABELS = ("fb0", "fb1", "verify", "probe", "null")
# The drifting pilot stream is shared by four runs; the no-drift one belongs to `null` alone.
PILOT_STREAM = ("fu-pilot-01", 4301)
PILOT_NULL_STREAM = ("fu-pilot-null-01", 4311)
PILOT_STREAM_IDS = [PILOT_STREAM[0], PILOT_NULL_STREAM[0]]


def _pilot_config(label: str) -> ExperimentConfig:
    """The real pilot yaml of `label`, cut to one stream of `SESSIONS` sessions."""
    raw = load_experiment(FOLLOWUP / f"fu_pilot_{label}.yaml").model_dump()
    stream_id, seed = PILOT_NULL_STREAM if label == "null" else PILOT_STREAM
    raw |= {
        "stream_ids": [stream_id],
        "seeds": [seed],
        "sessions": SESSIONS,
        "questions_per_session": QUESTIONS,
        "probe_sessions": [SESSIONS],
        "probes_per_checkpoint": 2,
    }
    return ExperimentConfig.model_validate(raw)


@dataclass(frozen=True)
class FollowupPilot:
    configs: dict[str, ExperimentConfig]
    run_dirs: dict[str, Path]
    transports: dict[str, PolicyTransport]
    ledger: Path
    streams_root: Path


@pytest.fixture(scope="module")
def followup_pilot(tmp_path_factory: pytest.TempPathFactory) -> FollowupPilot:
    root = tmp_path_factory.mktemp("followup-pilot")
    configs = {label: _pilot_config(label) for label in PILOT_LABELS}
    streams_root = root / "streams"
    # Streams are generated once, from the fb0 and null configs; the other runs reuse them.
    generate_streams(configs["fb0"], streams_root)
    generate_streams(configs["null"], streams_root)
    ledger_file = root / "ledger.sqlite"
    ledger = CostLedger(ledger_file, phase="fu_pilot", phase_cap_usd=6.0, hard_cap_usd=200.0)
    transports: dict[str, PolicyTransport] = {}
    run_dirs: dict[str, Path] = {}
    for label, config in configs.items():
        # Question texts repeat across streams with other gold SQL, so each run gets its own.
        gold = _gold_by_question(load_streams(config, streams_root).values())
        transports[label] = PolicyTransport(gold)
        run_dirs[label] = run_experiment(
            config,
            out_root=root / "outputs",
            streams_root=streams_root,
            clbench_root=root / "no-clbench",
            client=MeteredClient(transports[label], ledger, load_models(MODELS)),
        )
    return FollowupPilot(configs, run_dirs, transports, ledger_file, streams_root)


def _followup_gate(pilot: FollowupPilot) -> dict[str, Any]:
    """The gate on the pilot runs, projecting onto the real main configurations."""
    main_jobs, phase_caps = count_main_jobs(
        {label: load_experiment(FOLLOWUP / f"fu_{label}.yaml") for label in PILOT_LABELS}
    )
    return evaluate_followup_gate(
        pilot.run_dirs,
        ledger=pilot.ledger,
        # Only the pilot streams exist here; the main streams are generated before the real gate.
        validation_problems=stream_validation_problems(PILOT_STREAM_IDS, pilot.streams_root),
        main_jobs=main_jobs,
        phase_caps=phase_caps,
    )


def test_e2e_fake_followup_pilot_gate(followup_pilot: FollowupPilot) -> None:
    jobs = {
        label: load_run(run_dir, followup_pilot.ledger).status
        for label, run_dir in followup_pilot.run_dirs.items()
    }

    gate = _followup_gate(followup_pilot)

    conditions = gate["conditions"]
    assert list(conditions) == [f"G{n}" for n in range(8)]
    assert gate["failed"] == []
    assert gate["passed"] is True
    # G0: all 5 + 4 + 3 + 1 + 2 jobs of the five runs completed, none is missing.
    assert {label: len(status) for label, status in jobs.items()} == {
        "fb0": 5,
        "fb1": 4,
        "verify": 3,
        "probe": 1,
        "null": 2,
    }
    assert all(state == "completed" for status in jobs.values() for state in status.values())
    assert conditions["G0"] == {"passed": True, "unfinished_jobs": {}}
    # G1: the generated pilot streams validate.
    assert conditions["G1"]["streams_checked"] == len(PILOT_STREAM_IDS)
    assert conditions["G1"]["problems"] == {}
    # G2: fb1 graded 4 arms x 2 sessions x 3 questions and wrote one feedback line for each;
    # fb0 wrote none.
    assert conditions["G2"]["fb1_graded_questions"] == 4 * SESSIONS * QUESTIONS
    assert conditions["G2"]["fb1_questions_with_feedback"] == 4 * SESSIONS * QUESTIONS
    assert conditions["G2"]["fb0_feedback_lines"] == 0
    # G3: A1 is in four runs and A4 in five; every question got a note, every consolidation
    # (one per stream and arm, after session 1) read one.
    assert conditions["G3"]["A1"] | {"passed": True} == {
        "questions": 4 * SESSIONS * QUESTIONS,
        "note_rate": 1.0,
        "consolidations_after_notes": 4,
        "notes_read_rate": 1.0,
        "passed": True,
    }
    assert conditions["G3"]["A4"]["questions"] == 5 * SESSIONS * QUESTIONS
    assert (conditions["G3"]["A4"]["note_rate"], conditions["G3"]["A4"]["notes_read_rate"]) == (
        1.0,
        1.0,
    )
    # G4: A2 sits in fb0 and fb1; the questions of session 2 all got excerpts.
    assert (conditions["G4"]["questions"], conditions["G4"]["retrieval_rate"]) == (
        2 * QUESTIONS,
        1.0,
    )
    # G5: every answer was submitted in each of the 15 (run, arm) pairs.
    assert set(conditions["G5"]["no_submission_rate"].values()) == {0.0}
    assert len(conditions["G5"]["no_submission_rate"]) == 15
    # G6: every main condition has a pilot price.
    assert conditions["G6"]["unpriced"] == []
    # G7: the probe run's only consolidation ran a query.
    assert (conditions["G7"]["consolidations"], conditions["G7"]["query_rate"]) == (1, 1.0)


def test_e2e_fake_verify_addendum_reaches_only_the_verify_run(
    followup_pilot: FollowupPilot,
) -> None:
    addendum = load_prompt("agent_verify_addendum.md")

    def online_system_prompts(label: str) -> list[str]:
        return [
            call["messages"][0]["content"]
            for call in followup_pilot.transports[label].calls
            if any(tool["function"]["name"] == "submit_sql" for tool in call["tools"] or [])
        ]

    assert online_system_prompts("verify")
    assert all(addendum in prompt for prompt in online_system_prompts("verify"))
    others = [online_system_prompts(label) for label in ("fb0", "fb1", "probe", "null")]
    assert all(prompts and addendum not in "".join(prompts) for prompts in others)


def test_e2e_fake_runs_of_different_configs_read_the_same_stream_files(
    followup_pilot: FollowupPilot,
) -> None:
    # The drifting pilot stream was generated from the fb0 config only; the fb1, verify and
    # probe runs started from it, and their manifests pin the very same stream.json.
    hashes = {
        label: load_manifest(followup_pilot.run_dirs[label]).input_sha256
        for label in ("fb0", "fb1", "verify", "probe")
    }

    assert set(hashes["fb0"]) == {PILOT_STREAM[0]}
    assert hashes["fb1"] == hashes["verify"] == hashes["probe"] == hashes["fb0"]


def test_e2e_fake_no_drift_stream(tmp_path: Path) -> None:
    # Five sessions reach epoch 2, where a drifting stream would have changed its schema.
    sessions = 5
    raw = _pilot_config("null").model_dump() | {"sessions": sessions, "probe_sessions": [sessions]}
    config = ExperimentConfig.model_validate(raw)
    streams_root = tmp_path / "streams"
    generate_streams(config, streams_root)
    stream = load_streams(config, streams_root)[PILOT_NULL_STREAM[0]]
    ledger_file = tmp_path / "ledger.sqlite"
    ledger = CostLedger(ledger_file, phase="fu_pilot", phase_cap_usd=6.0, hard_cap_usd=200.0)
    client = MeteredClient(
        PolicyTransport(_gold_by_question([stream])), ledger, load_models(MODELS)
    )

    run_dir = run_experiment(
        config,
        out_root=tmp_path / "outputs",
        streams_root=streams_root,
        clbench_root=tmp_path / "no-clbench",
        client=client,
    )

    assert stream.drift is False
    assert incomplete_jobs(config, run_dir) == {}
    data = load_run(run_dir, ledger_file)
    assert len(data.results) == 2 * sessions * QUESTIONS
    assert not any(result.drift_dependent for result in data.results)
    assert all(result.note_written for result in data.results)
    # The schema of epoch 2 is epoch 1's, while the epochs go on adding rows.
    assert _schema(stream, 2) == _schema(stream, 1)
    assert _row_count(stream, 2) > _row_count(stream, 1)


def test_runner_refuses_a_no_drift_stream_for_a_drifting_config(tmp_path: Path) -> None:
    null_config = _pilot_config("null")
    streams_root = tmp_path / "streams"
    generate_streams(null_config, streams_root)
    drifting = ExperimentConfig.model_validate(null_config.model_dump() | {"drift": True})
    client = MeteredClient(
        PolicyTransport({}),
        CostLedger(
            tmp_path / "ledger.sqlite", phase="fu_pilot", phase_cap_usd=6.0, hard_cap_usd=200.0
        ),
        load_models(MODELS),
    )

    with pytest.raises(ValueError, match="drift"):
        run_experiment(
            drifting,
            out_root=tmp_path / "outputs",
            streams_root=streams_root,
            clbench_root=tmp_path / "no-clbench",
            client=client,
        )

    assert not (tmp_path / "outputs").exists()


def _schema(stream: Stream, epoch: int) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(stream.db_path(epoch))) as db:
        rows = db.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    return [(name, sql) for name, sql in rows]


def _row_count(stream: Stream, epoch: int) -> int:
    with closing(sqlite3.connect(stream.db_path(epoch))) as db:
        tables = [
            name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        return sum(db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables)
