"""The follow-up pilot gate G0-G7 (docs/followup/00_design.md 7) and its command line."""

import importlib.util
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from project_module.config import ArmName, Budgets, ExperimentConfig, Phase
from project_module.contracts import ConsolidationRecord, GradeCategory, QuestionResult, RunStatus
from project_module.evaluation.followup_gate import (
    COST_SAFETY,
    count_main_jobs,
    evaluate_followup_gate,
)
from project_module.layout import RunLayout
from project_module.manifest import RunManifest, config_sha256, write_manifest
from project_module.schemadrift.generator import generate_stream
from project_module.transcript import TranscriptWriter

from .builders import write_jsonl, write_ledger

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "analyze_followup.py"

PILOT_ARMS: dict[str, tuple[ArmName, ...]] = {
    "fb0": ("A0", "A1", "A2", "A4", "A5"),
    "fb1": ("A0", "A1", "A2", "A4"),
    "verify": ("A0", "A1", "A4"),
    "probe": ("A4",),
    "null": ("A1", "A4"),
}
PILOT_STREAMS = {"null": ("fu-pilot-null-01",)}
DEFAULT_STREAMS = ("fu-pilot-01", "fu-pilot-02")
SESSIONS = 2
QUESTIONS = 10
ROWS_PER_JOB = SESSIONS * QUESTIONS
JOB_COST_USD = 0.05


@dataclass(frozen=True)
class Pilot:
    """Knobs of the synthetic pilot; the defaults meet every gate condition.

    Counts are per job, out of its 20 questions (or its consolidations).
    """

    notes_written: Mapping[ArmName, int] = field(default_factory=dict)
    fb0_feedback: bool = False
    fb1_feedback: bool = True
    a2_retrieved_after_first: int = QUESTIONS
    no_submissions: int = 0
    notes_files_read: int = 1
    probe_sql_queries: int = 1
    status: Mapping[tuple[str, str, str], RunStatus] = field(default_factory=dict)

    def note_count(self, arm: ArmName) -> int:
        return self.notes_written.get(arm, ROWS_PER_JOB)


def _row(stream_id: str, arm: ArmName, session: int, index: int, pilot: Pilot) -> QuestionResult:
    category: GradeCategory = "no_submission" if index < pilot.no_submissions else "correct"
    retrieved = arm == "A2" and session > 1 and index - QUESTIONS < pilot.a2_retrieved_after_first
    return QuestionResult(
        stream_id=stream_id,
        arm=arm,
        session=session,
        qid=f"s{session:02d}q{index}",
        category=category,
        score=float(category == "correct"),
        queries=2,
        ok_queries=2,
        tool_calls=2,
        tool_errors=0,
        turns=3,
        drift_dependent=False,
        injected_target=False,
        note_written=arm in ("A1", "A4") and index < pilot.note_count(arm),
        retrieved_tokens=100 if retrieved else 0,
    )


def _record(
    stream_id: str, arm: ArmName, session: int, *, probe: bool, pilot: Pilot
) -> ConsolidationRecord:
    return ConsolidationRecord(
        stream_id=stream_id,
        arm=arm,
        session=session,
        phases=["analyzer", "organizer"],
        turns=4,
        input_tokens=1000,
        output_tokens=100,
        stopped="final",
        injected_updated=True,
        notes_files_read=pilot.notes_files_read,
        sql_queries=pilot.probe_sql_queries if probe else 0,
    )


def _write_manifest(
    run_dir: Path, *, arms: tuple[ArmName, ...], stream_ids: tuple[str, ...]
) -> None:
    config = ExperimentConfig(
        name=run_dir.name,
        phase="fu_pilot",
        arms=list(arms),
        stream_ids=list(stream_ids),
        seeds=[4301 + i for i in range(len(stream_ids))],
        inject_false_notes=False,
        session_notes=True,
        agent_model="m",
        consolidator_model="m",
        embedding_model="e",
        budgets=Budgets(phase_cap_usd=6.0),
    )
    write_manifest(
        RunLayout(run_dir),
        RunManifest(
            run_id=run_dir.name,
            phase="fu_pilot",
            created_utc="2026-11-11T00:00:00+00:00",
            git_sha=None,
            git_dirty=None,
            config_sha256=config_sha256(config),
            config=config,
            input_sha256={},
        ),
    )


def _write_transcript(path: Path, stream_id: str, session: int, *, feedback: bool) -> None:
    with TranscriptWriter(path, stream_id=stream_id, session=session) as writer:
        for index in range(QUESTIONS):
            writer.user(f"Question {index}?")
            writer.tool_call("query", {"sql": "select 1"})
            writer.tool_result("query", "1\n1")
            writer.tool_call("submit_sql", {"sql": "select 1"})
            if feedback:
                writer.event("feedback", f"Question {index + 1}: CORRECT!")


def _write_pilot(tmp_path: Path, pilot: Pilot) -> tuple[dict[str, Path], Path]:
    """Five pilot run directories and a ledger with one cost row per job."""
    run_dirs: dict[str, Path] = {}
    ledger_rows: list[tuple[Any, ...]] = []
    for label, arms in PILOT_ARMS.items():
        run_dir = tmp_path / f"fu_pilot_{label}-20261111T000000Z"
        layout = RunLayout(run_dir)
        stream_ids = PILOT_STREAMS.get(label, DEFAULT_STREAMS)
        _write_manifest(run_dir, arms=arms, stream_ids=stream_ids)
        feedback = {"fb0": pilot.fb0_feedback, "fb1": pilot.fb1_feedback}.get(label, False)
        for stream_id in stream_ids:
            for arm in arms:
                for session in range(1, SESSIONS + 1):
                    first = (session - 1) * QUESTIONS
                    rows = [
                        _row(stream_id, arm, session, first + i, pilot) for i in range(QUESTIONS)
                    ]
                    write_jsonl(layout.results_file(stream_id, arm, session), rows)
                    _write_transcript(
                        layout.transcript_file(stream_id, arm, session),
                        stream_id,
                        session,
                        feedback=feedback,
                    )
                    if arm in ("A1", "A4"):
                        record = _record(
                            stream_id, arm, session, probe=label == "probe", pilot=pilot
                        )
                        write_jsonl(layout.consolidation_file(stream_id, arm, session), [record])
                status = pilot.status.get((label, stream_id, arm), "completed")
                layout.status_file(stream_id, arm).write_text(
                    json.dumps({"status": status, "completed_sessions": SESSIONS}), encoding="utf-8"
                )
                ledger_rows.append(
                    (
                        f"{label}-{stream_id}-{arm}",
                        "fu_pilot",
                        run_dir.name,
                        stream_id,
                        arm,
                        1,
                        "online",
                        JOB_COST_USD,
                        JOB_COST_USD,
                        "settled",
                    )
                )
        run_dirs[label] = run_dir
    ledger = tmp_path / "ledger.sqlite"
    write_ledger(ledger, ledger_rows)
    return run_dirs, ledger


MAIN_JOBS = {
    "fu_main": {f"{label}/{arm}": 40 for label in ("fb0", "fb1") for arm in PILOT_ARMS[label]},
    "fu_aux": {
        "verify/A0": 20,
        "verify/A1": 20,
        "verify/A4": 20,
        "probe/A4": 20,
        "null/A1": 10,
        "null/A4": 10,
    },
}
PHASE_CAPS = {"fu_main": 52.0, "fu_aux": 25.0}


def _gate(tmp_path: Path, pilot: Pilot = Pilot(), **overrides: Any) -> dict[str, Any]:
    run_dirs, ledger = _write_pilot(tmp_path, pilot)
    arguments: dict[str, Any] = {
        "ledger": ledger,
        "validation_problems": {"fu-pilot-01": [], "fu-01": []},
        "main_jobs": MAIN_JOBS,
        "phase_caps": PHASE_CAPS,
    }
    return evaluate_followup_gate(run_dirs, **(arguments | overrides))


def _failed(gate: dict[str, Any]) -> list[str]:
    return [name for name, condition in gate["conditions"].items() if not condition["passed"]]


def test_gate_passes_on_all_mechanical_conditions(tmp_path: Path) -> None:
    gate = _gate(tmp_path)

    assert _failed(gate) == []
    assert gate["passed"] is True
    assert list(gate["conditions"]) == [f"G{i}" for i in range(8)]


def test_gate_fails_on_low_note_rate(tmp_path: Path) -> None:
    gate = _gate(tmp_path, Pilot(notes_written={"A1": 17}))

    assert _failed(gate) == ["G3"]
    assert gate["conditions"]["G3"]["A1"]["note_rate"] == pytest.approx(0.85)
    assert gate["passed"] is False


def test_gate_requires_no_feedback_in_fb0(tmp_path: Path) -> None:
    gate = _gate(tmp_path, Pilot(fb0_feedback=True))

    assert _failed(gate) == ["G2"]
    assert gate["conditions"]["G2"]["fb0_feedback_lines"] == 5 * 2 * SESSIONS * QUESTIONS


def test_gate_requires_feedback_for_every_fb1_question(tmp_path: Path) -> None:
    gate = _gate(tmp_path, Pilot(fb1_feedback=False))

    assert _failed(gate) == ["G2"]
    assert gate["conditions"]["G2"]["fb1_questions_with_feedback"] == 0


@pytest.mark.parametrize(
    ("pilot", "condition"),
    [
        (Pilot(status={("verify", "fu-pilot-02", "A1"): "failed"}), "G0"),
        (Pilot(notes_files_read=0), "G3"),
        (Pilot(a2_retrieved_after_first=QUESTIONS - 1), "G4"),
        (Pilot(no_submissions=3), "G5"),
        (Pilot(probe_sql_queries=0), "G7"),
    ],
    ids=["unfinished-job", "notes-not-read", "retrieval-gap", "no-submission", "probe-idle"],
)
def test_gate_fails_on_each_mechanical_condition(
    tmp_path: Path, pilot: Pilot, condition: str
) -> None:
    gate = _gate(tmp_path, pilot)

    assert condition in _failed(gate)
    assert gate["passed"] is False


def test_gate_fails_on_stream_validation_problem(tmp_path: Path) -> None:
    gate = _gate(tmp_path, validation_problems={"fu-07": ["s05q2: empty expected result"]})

    assert _failed(gate) == ["G1"]
    assert gate["conditions"]["G1"]["problems"] == {"fu-07": ["s05q2: empty expected result"]}


def test_gate_projects_main_cost_with_safety_factor(tmp_path: Path) -> None:
    gate = _gate(tmp_path, phase_caps={"fu_main": 1.0, "fu_aux": 25.0})

    projected = gate["conditions"]["G6"]["projected_usd"]
    # fu_main: 9 conditions x 40 jobs x $0.05 per pilot job x 1.3.
    assert projected["fu_main"] == pytest.approx(9 * 40 * JOB_COST_USD * COST_SAFETY)
    assert _failed(gate) == ["G6"]


def test_gate_cost_needs_a_pilot_price_for_every_main_condition(tmp_path: Path) -> None:
    main_jobs = {"fu_aux": {"sens/A4": 10}}

    gate = _gate(tmp_path, main_jobs=main_jobs, phase_caps={"fu_aux": 25.0})

    assert _failed(gate) == ["G6"]
    assert gate["conditions"]["G6"]["unpriced"] == ["sens/A4"]


def test_gate_rejects_missing_pilot_run(tmp_path: Path) -> None:
    run_dirs, ledger = _write_pilot(tmp_path, Pilot())
    del run_dirs["probe"]

    with pytest.raises(ValueError, match="probe"):
        evaluate_followup_gate(
            run_dirs,
            ledger=ledger,
            validation_problems={},
            main_jobs=MAIN_JOBS,
            phase_caps=PHASE_CAPS,
        )


def _main_config(
    name: str,
    phase: Phase,
    arms: list[ArmName],
    stream_ids: list[str],
    cap: float,
    *,
    overrides: dict[str, list[ArmName]] | None = None,
) -> ExperimentConfig:
    return ExperimentConfig(
        name=name,
        phase=phase,
        arms=arms,
        arm_overrides=overrides or {},
        stream_ids=stream_ids,
        seeds=[4001 + i for i in range(len(stream_ids))],
        inject_false_notes=False,
        agent_model="m",
        consolidator_model="m",
        embedding_model="e",
        budgets=Budgets(phase_cap_usd=cap),
    )


def test_count_main_jobs_counts_streams_per_arm_and_reads_caps() -> None:
    configs = {
        "fb0": _main_config(
            "fu_fb0",
            "fu_main",
            ["A0", "A4"],
            ["fu-01", "fu-02", "fu-03"],
            52.0,
            overrides={"fu-01": ["A4"]},
        ),
        "probe": _main_config("fu_probe", "fu_aux", ["A4"], ["fu-01", "fu-02"], 25.0),
    }

    jobs, caps = count_main_jobs(configs)

    assert jobs == {"fu_main": {"fb0/A0": 2, "fb0/A4": 3}, "fu_aux": {"probe/A4": 2}}
    assert caps == {"fu_main": 52.0, "fu_aux": 25.0}


def test_count_main_jobs_rejects_conflicting_caps() -> None:
    configs = {
        "fb0": _main_config("fu_fb0", "fu_main", ["A0"], ["fu-01"], 52.0),
        "fb1": _main_config("fu_fb1", "fu_main", ["A0"], ["fu-01"], 50.0),
    }

    with pytest.raises(ValueError, match="fu_main"):
        count_main_jobs(configs)


# --- scripts/analyze_followup.py ------------------------------------------------------------


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("analyze_followup", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_main_config(path: Path, config: ExperimentConfig) -> None:
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")


# Main configurations of the five pilot labels, valid for the gate unless a test overrides one.
MAIN_CONFIGS: dict[str, ExperimentConfig] = {
    "fb0": _main_config("fu_fb0", "fu_main", ["A0", "A4"], ["fu-01", "fu-02"], 52.0),
    "fb1": _main_config("fu_fb1", "fu_main", ["A0", "A4"], ["fu-01", "fu-02"], 52.0),
    "verify": _main_config("fu_verify", "fu_aux", ["A0", "A4"], ["fu-01"], 25.0),
    "probe": _main_config("fu_probe", "fu_aux", ["A4"], ["fu-02"], 25.0),
    "null": _main_config("fu_null", "fu_aux", ["A1", "A4"], ["fu-null-01"], 25.0),
}


def _main_config_argv(tmp_path: Path, configs: Mapping[str, ExperimentConfig]) -> list[str]:
    argv: list[str] = []
    for label, config in configs.items():
        path = tmp_path / f"main-{label}.yaml"
        _write_main_config(path, config)
        argv.append(f"--main-config={label}={path}")
    return argv


def _gate_argv(
    run_dirs: Mapping[str, Path],
    ledger: Path,
    tmp_path: Path,
    configs: Mapping[str, ExperimentConfig] = MAIN_CONFIGS,
) -> list[str]:
    argv = [f"--run={label}={run_dir}" for label, run_dir in run_dirs.items()]
    return [
        *argv,
        "--gate",
        *_main_config_argv(tmp_path, configs),
        f"--streams={tmp_path / 'streams'}",
        f"--ledger={ledger}",
        f"--out={tmp_path / 'out'}",
    ]


def test_script_gate_passes_with_valid_streams(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dirs, ledger = _write_pilot(tmp_path, Pilot())
    for seed, stream_id in enumerate((*DEFAULT_STREAMS, "fu-01", "fu-02"), start=4501):
        generate_stream(stream_id, seed, tmp_path / "streams" / stream_id, inject=False)
    for seed, stream_id in enumerate((*PILOT_STREAMS["null"], "fu-null-01"), start=4601):
        generate_stream(
            stream_id, seed, tmp_path / "streams" / stream_id, inject=False, drift=False
        )

    code = script.main(_gate_argv(run_dirs, ledger, tmp_path))

    gate = json.loads((tmp_path / "out" / "followup-gate.json").read_text(encoding="utf-8"))
    assert code == 0
    assert gate["passed"] is True
    assert "gate passed: True" in capsys.readouterr().out


def test_script_gate_fails_without_generated_streams(script: ModuleType, tmp_path: Path) -> None:
    run_dirs, ledger = _write_pilot(tmp_path, Pilot())

    code = script.main(_gate_argv(run_dirs, ledger, tmp_path))

    gate = json.loads((tmp_path / "out" / "followup-gate.json").read_text(encoding="utf-8"))
    assert code == 1
    assert gate["conditions"]["G1"]["problems"]["fu-01"] == ["stream not generated"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--run=fb0", "--out=o"],
        ["--run==dir", "--out=o"],
        ["--run=Fb0/x=dir", "--out=o"],
        ["--run=fb0=a", "--run=fb0=b", "--out=o"],
        ["--run=fb0=a", "--gate", "--out=o"],
        ["--run=fb0=a", "--main-config=fb0=c.yaml", "--out=o"],
    ],
    ids=[
        "no-equals",
        "empty-label",
        "bad-label",
        "duplicate",
        "gate-without-main",
        "main-without-gate",
    ],
)
def test_script_rejects_bad_arguments(script: ModuleType, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        script.main(argv)

    assert exit_info.value.code == 2


def _with(label: str, **update: Any) -> dict[str, ExperimentConfig]:
    return MAIN_CONFIGS | {label: MAIN_CONFIGS[label].model_copy(update=update)}


@pytest.mark.parametrize(
    ("configs", "message"),
    [
        ({k: v for k, v in MAIN_CONFIGS.items() if k != "probe"}, "missing: probe"),
        (MAIN_CONFIGS | {"sens": MAIN_CONFIGS["verify"]}, "unexpected: sens"),
        (_with("fb1", phase="fu_aux"), "fb1 must be phase fu_main"),
        (_with("null", phase="fu_main"), "null must be phase fu_aux"),
        (_with("verify", stream_ids=["fu-03"], seeds=[4003]), "verify streams"),
        (_with("probe", stream_ids=["fu-01", "fu-03"], seeds=[4001, 4003]), "probe streams"),
        (_with("fb1", stream_ids=["fu-01"], seeds=[4001]), "fb1 streams"),
    ],
    ids=[
        "missing-label",
        "extra-label",
        "fb1-wrong-phase",
        "null-wrong-phase",
        "verify-not-subset",
        "probe-not-subset",
        "fb1-not-equal",
    ],
)
def test_script_gate_rejects_main_configs_that_do_not_match_the_design(
    script: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    configs: dict[str, ExperimentConfig],
    message: str,
) -> None:
    run_dirs, ledger = _write_pilot(tmp_path, Pilot())

    with pytest.raises(SystemExit) as exit_info:
        script.main(_gate_argv(run_dirs, ledger, tmp_path, configs))

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out" / "followup-gate.json").exists()
