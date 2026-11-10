import json

import pytest
from fakes import make_tag
from project_module.config import ModelSpec
from project_module.contracts import Usage
from project_module.evaluation.load import analyzable_streams, load_run
from project_module.layout import RunLayout
from project_module.llm.ledger import CostLedger

from .builders import (
    make_consolidation,
    make_probe,
    make_result,
    make_run,
    write_jsonl,
    write_ledger,
    write_run_manifest,
)

RUN_ID = "pilot-20261110T000000Z"


def _write_status(layout, stream_id, arm, status, completed_sessions):
    path = layout.status_file(stream_id, arm)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"status": status, "completed_sessions": completed_sessions}), encoding="utf-8"
    )


def _ledger_rows():
    # (id, phase, run_id, stream_id, arm, session, component, reserved, actual, state)
    return [
        ("r1", "pilot", RUN_ID, "s1", "A0", 1, "online", 0.10, 0.04, "settled"),
        ("r2", "pilot", RUN_ID, "s1", "A0", 1, "online", 0.10, 0.06, "settled"),
        ("r3", "pilot", RUN_ID, "s1", "A0", 1, "online", 0.10, None, "forfeited"),
        ("r4", "pilot", RUN_ID, "s1", "A0", 1, "online", 0.10, None, "released"),
        ("r5", "pilot", RUN_ID, "s1", "A0", 2, "online", 0.30, None, "open"),
        ("r6", "pilot", RUN_ID, "s1", "A1", 1, "consolidation", 0.50, 0.20, "settled"),
        ("other", "pilot", "another-run", "s1", "A0", 1, "online", 9.0, 9.0, "settled"),
    ]


def _build_run_dir(tmp_path):
    run_dir = tmp_path / RUN_ID
    layout = RunLayout(run_dir)
    write_jsonl(
        layout.results_file("s1", "A0", 1),
        [make_result("s1", "A0", 1, "q1"), make_result("s1", "A0", 1, "q2", "wrong_result")],
    )
    write_jsonl(layout.results_file("s1", "A0", 2), [make_result("s1", "A0", 2, "q1", "stale")])
    _write_status(layout, "s1", "A0", "completed", 2)
    write_jsonl(layout.results_file("s1", "A1", 1), [make_result("s1", "A1", 1, "q1")])
    write_jsonl(layout.consolidation_file("s1", "A1", 1), [make_consolidation("s1", "A1", 1)])
    write_jsonl(
        layout.probes_file("s1", "A1", 1),
        [make_probe("s1", "A1", 1, "f1", correct=True, injected=False)],
    )
    _write_status(layout, "s1", "A1", "failed", 1)
    write_run_manifest(run_dir, arms=["A0", "A1"], stream_ids=["s1", "s2"])
    return run_dir


def test_load_run_reads_every_stream_arm_and_session(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    data = load_run(run_dir, tmp_path / "ledger.sqlite")

    assert sorted((r.stream_id, r.arm, r.session, r.qid) for r in data.results) == [
        ("s1", "A0", 1, "q1"),
        ("s1", "A0", 1, "q2"),
        ("s1", "A0", 2, "q1"),
        ("s1", "A1", 1, "q1"),
    ]
    assert [(p.arm, p.fact_id) for p in data.probes] == [("A1", "f1")]
    assert [(c.arm, c.session) for c in data.consolidations] == [("A1", 1)]
    assert data.status == {"s1/A0": "completed", "s1/A1": "failed"}
    assert data.required_jobs == ["s1/A0", "s1/A1", "s2/A0", "s2/A1"]


def test_load_run_takes_required_jobs_from_the_manifest_with_overrides(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    write_run_manifest(
        run_dir, arms=["A0", "A1"], stream_ids=["s1", "s2"], arm_overrides={"s2": ["A5"]}
    )
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    data = load_run(run_dir, tmp_path / "ledger.sqlite")

    assert data.required_jobs == ["s1/A0", "s1/A1", "s2/A5"]


def test_load_run_rejects_a_run_without_manifest(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    (run_dir / "manifest.json").unlink()
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    with pytest.raises(FileNotFoundError, match="manifest"):
        load_run(run_dir, tmp_path / "ledger.sqlite")


def test_load_run_deduplicates_rows(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    layout = RunLayout(run_dir)
    # A leftover copy of session 1 in a second file, plus a changed duplicate inside one file.
    write_jsonl(
        layout.results_file("s1", "A0", 1).with_name("session_001_copy.jsonl"),
        [make_result("s1", "A0", 1, "q1")],
    )
    write_jsonl(
        layout.probes_file("s1", "A1", 1),
        [
            make_probe("s1", "A1", 1, "f1", correct=False, injected=False),
            make_probe("s1", "A1", 1, "f1", correct=True, injected=False),
        ],
    )
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    data = load_run(run_dir, tmp_path / "ledger.sqlite")

    keys = [(r.stream_id, r.arm, r.session, r.qid) for r in data.results]
    assert len(keys) == len(set(keys)) == 4
    assert [p.correct for p in data.probes] == [True]


def test_load_run_marks_a_job_without_status_file_as_running(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    layout = RunLayout(run_dir)
    write_jsonl(layout.results_file("s2", "A0", 1), [make_result("s2", "A0", 1, "q1")])
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    data = load_run(run_dir, tmp_path / "ledger.sqlite")

    assert data.status["s2/A0"] == "running"


def test_load_run_costs_follow_the_ledger_cost_rule(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    data = load_run(run_dir, tmp_path / "ledger.sqlite")

    by_cell = {(c["stream_id"], c["arm"], c["session"], c["component"]): c for c in data.costs}
    # settled -> actual (0.04 + 0.06), forfeited -> reserved (0.10), released -> 0.
    assert by_cell[("s1", "A0", 1, "online")]["cost_usd"] == pytest.approx(0.20)
    assert by_cell[("s1", "A0", 1, "online")]["calls"] == 4
    # open -> reserved
    assert by_cell[("s1", "A0", 2, "online")]["cost_usd"] == pytest.approx(0.30)
    assert by_cell[("s1", "A1", 1, "consolidation")]["cost_usd"] == pytest.approx(0.20)
    # Only this run's rows are read.
    assert sum(c["cost_usd"] for c in data.costs) == pytest.approx(0.70)


def test_load_run_reads_a_wal_mode_ledger(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows(), wal=True)

    data = load_run(run_dir, tmp_path / "ledger.sqlite")

    assert sum(c["cost_usd"] for c in data.costs) == pytest.approx(0.70)


def test_load_run_does_not_modify_the_ledger(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    ledger = tmp_path / "ledger.sqlite"
    write_ledger(ledger, _ledger_rows())
    before = ledger.read_bytes()

    load_run(run_dir, ledger)

    assert ledger.read_bytes() == before


def test_load_run_rejects_a_missing_ledger(tmp_path):
    run_dir = _build_run_dir(tmp_path)

    with pytest.raises(FileNotFoundError, match="ledger"):
        load_run(run_dir, tmp_path / "missing.sqlite")


def test_load_run_rejects_a_missing_run_dir(tmp_path):
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    with pytest.raises(FileNotFoundError, match="run directory"):
        load_run(tmp_path / "missing", tmp_path / "ledger.sqlite")


def test_load_run_rejects_a_malformed_result_row(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    layout = RunLayout(run_dir)
    layout.results_file("s1", "A0", 3).write_text('{"stream_id": "s1"}\n', encoding="utf-8")
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    with pytest.raises(ValueError, match="session_003"):
        load_run(run_dir, tmp_path / "ledger.sqlite")


def test_analyzable_streams_require_all_arms_completed():
    data = make_run(
        status={
            "s1/A0": "completed",
            "s1/A1": "completed",
            "s2/A0": "completed",
            "s2/A1": "failed",
            "s3/A0": "completed",
            "s3/A1": "budget_exhausted",
            "s4/A0": "running",
            "s4/A1": "completed",
            "s5/A0": "completed",
        }
    )

    assert analyzable_streams(data, ["A0", "A1"]) == ["s1"]
    assert analyzable_streams(data, ["A0"]) == ["s1", "s2", "s3", "s5"]


def test_analyzable_streams_drop_streams_that_lack_an_arm_job():
    data = make_run(status={"s1/A0": "completed", "s1/A1": "completed", "s2/A0": "completed"})

    assert analyzable_streams(data, ["A0", "A1"]) == ["s1"]


def test_load_run_ignores_blank_lines(tmp_path):
    run_dir = _build_run_dir(tmp_path)
    layout = RunLayout(run_dir)
    path = layout.results_file("s1", "A0", 1)
    path.write_text(path.read_text(encoding="utf-8") + "\n   \n", encoding="utf-8")
    write_ledger(tmp_path / "ledger.sqlite", _ledger_rows())

    data = load_run(run_dir, tmp_path / "ledger.sqlite")

    assert len(data.results) == 4


def test_load_run_costs_equal_the_real_ledger_committed_spend(tmp_path):
    spec = ModelSpec(
        deployment="m",
        input_usd_per_m=1.0,
        cached_input_usd_per_m=0.1,
        output_usd_per_m=2.0,
        kind="chat",
    )
    ledger_file = tmp_path / "ledger.sqlite"
    ledger = CostLedger(ledger_file, phase="pilot", phase_cap_usd=10.0, hard_cap_usd=20.0)
    tag = make_tag(phase="pilot", run_id=RUN_ID, stream_id="s1", arm="A0")
    settled = ledger.reserve(spec, input_tokens=1000, max_output_tokens=500, tag=tag)
    ledger.settle(settled, Usage(input_tokens=900, cached_input_tokens=100, output_tokens=200))
    ledger.forfeit(ledger.reserve(spec, input_tokens=2000, max_output_tokens=100, tag=tag))
    ledger.release(ledger.reserve(spec, input_tokens=3000, max_output_tokens=100, tag=tag))
    ledger.reserve(spec, input_tokens=400, max_output_tokens=100, tag=tag)  # left open

    data = load_run(_build_run_dir(tmp_path), ledger_file)

    assert sum(c["cost_usd"] for c in data.costs) == pytest.approx(ledger.spent_usd(phase="pilot"))
