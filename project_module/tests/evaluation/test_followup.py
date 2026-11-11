"""Multi-run analysis of the follow-up study (20_preregistration 2-5)."""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from project_module.config import ArmName
from project_module.contracts import RunStatus
from project_module.evaluation.followup import (
    clbench_summary,
    common_streams,
    condition_scores,
    contrast,
    exclusions,
    fixed_sequence,
    interaction,
    merge_runs,
    partial_sessions,
    summarize_followup,
)
from project_module.evaluation.load import RunData, job_key
from project_module.evaluation.stats import CI
from project_module.layout import RunLayout
from project_module.schemadrift.models import Fact
from project_module.transcript import TranscriptWriter

from .builders import (
    make_result,
    make_run,
    make_stream,
    write_jsonl,
    write_ledger,
    write_run_manifest,
)

FB0_A0 = ("fb0", "A0")
FB0_A4 = ("fb0", "A4")
FB1_A0 = ("fb1", "A0")
FB1_A4 = ("fb1", "A4")
FB0_A1 = ("fb0", "A1")
FB1_A1 = ("fb1", "A1")


def _scored_run(
    scores: Mapping[tuple[str, ArmName], Sequence[bool]],
    *,
    status: Mapping[str, RunStatus] | None = None,
    required: Sequence[str] | None = None,
) -> RunData:
    """One session-1 question per answer; every job with answers is completed unless overridden."""
    results = [
        make_result(stream_id, arm, 1, f"q{i}", "correct" if right else "wrong_result")
        for (stream_id, arm), answers in scores.items()
        for i, right in enumerate(answers)
    ]
    states: dict[str, RunStatus] = {job_key(s, a): "completed" for s, a in scores}
    states |= dict(status or {})
    return make_run(
        results=results,
        status=states,
        required_jobs=sorted(states) if required is None else required,
    )


def test_common_streams_drops_stream_missing_any_condition() -> None:
    runs = {
        "fb0": _scored_run(
            {(s, a): [True] for s in ("s1", "s2", "s3") for a in ("A0", "A4")},
        ),
        "fb1": _scored_run(
            {(s, a): [True] for s in ("s1", "s2") for a in ("A0", "A4")},
            status={"s2/A4": "interrupted"},
        ),
    }

    streams = common_streams(runs, [FB0_A0, FB0_A4, FB1_A0, FB1_A4])

    assert streams == ["s1"]


def test_exclusions_count_by_reason_and_condition() -> None:
    runs = {
        "fb0": _scored_run(
            {(s, a): [True] for s in ("s1", "s2", "s3") for a in ("A0", "A4")},
            status={"s2/A4": "failed", "s3/A4": "failed"},
        ),
        "fb1": _scored_run(
            {(s, a): [True] for s in ("s1", "s2") for a in ("A0", "A4")},
            status={"s3/A0": "budget_exhausted"},
            required=[job_key(s, a) for s in ("s1", "s2", "s3") for a in ("A0", "A4")],
        ),
    }

    counts = exclusions(runs, [FB0_A0, FB0_A4, FB1_A0, FB1_A4])

    assert counts == {
        "budget_exhausted": {"fb1/A0": 1},
        "failed": {"fb0/A4": 2},
        "not_started": {"fb1/A4": 1},
    }


def test_condition_scores_read_the_run_of_the_condition() -> None:
    runs = {
        "fb0": _scored_run({("s1", "A4"): [True, False]}),
        "fb1": _scored_run({("s1", "A4"): [True, True]}),
    }

    assert condition_scores(runs, FB1_A4, ["s1"]) == {"s1": 1.0}
    assert condition_scores(runs, FB0_A4, ["s1"]) == {"s1": 0.5}


def test_contrast_pairs_by_stream_across_runs() -> None:
    # Per-stream accuracy: fb1/A4 = 1.0, 1.0, 0.5 and fb0/A4 = 0.5, 0.5, 0.5 -> diffs 0.5, 0.5, 0.
    runs = {
        "fb0": _scored_run({(s, "A4"): [True, False] for s in ("s1", "s2", "s3")}),
        "fb1": _scored_run(
            {("s3", "A4"): [True, False], ("s1", "A4"): [True, True], ("s2", "A4"): [True, True]}
        ),
    }

    ci = contrast(runs, FB1_A4, FB0_A4, ["s1", "s2", "s3"])

    assert ci.n == 3
    assert ci.mean == pytest.approx(1 / 3)
    assert ci.low <= ci.mean <= ci.high


def test_contrast_without_paired_streams_names_the_conditions() -> None:
    runs = {"fb0": _scored_run({("s1", "A4"): [True]}), "fb1": _scored_run({("s2", "A4"): [True]})}

    with pytest.raises(ValueError, match="fb1/A4 - fb0/A4"):
        contrast(runs, FB1_A4, FB0_A4, ["s1", "s2"])


def test_interaction_is_difference_of_differences() -> None:
    # Each stream: (fb1/A4 - fb0/A4) - (fb1/A1 - fb0/A1) = (1 - 0.5) - (0.5 - 0.5) = 0.5.
    half = [True, False]
    runs = {
        "fb0": _scored_run({(s, a): half for s in ("s1", "s2") for a in ("A1", "A4")}),
        "fb1": _scored_run(
            {(s, "A4"): [True, True] for s in ("s1", "s2")}
            | {(s, "A1"): half for s in ("s1", "s2")}
        ),
    }

    ci = interaction(runs, FB1_A4, FB0_A4, FB1_A1, FB0_A1, ["s1", "s2"])

    assert ci == CI(mean=0.5, low=0.5, high=0.5, n=2)


def test_fixed_sequence_stops_confirmatory_after_first_failure() -> None:
    out = fixed_sequence(
        [
            ("H1", CI(mean=0.03, low=0.01, high=0.05, n=40)),
            ("H2", CI(mean=0.0, low=-0.02, high=0.02, n=40)),
            ("H3", CI(mean=0.05, low=0.02, high=0.08, n=40)),
        ],
        n_streams=40,
    )

    assert [r["confirmatory"] for r in out] == [True, True, False]
    assert [r["verdict"] for r in out] == ["supported", "not_shown", "supported"]


def test_fixed_sequence_reports_reversed_effect() -> None:
    out = fixed_sequence([("H1", CI(mean=-0.05, low=-0.08, high=-0.02, n=40))], n_streams=40)

    assert out[0]["verdict"] == "reversed"
    assert out[0]["confirmatory"] is True


def test_fixed_sequence_with_missing_streams_is_exploratory() -> None:
    supported = CI(mean=0.1, low=0.05, high=0.15, n=39)

    out = fixed_sequence([("H1", supported), ("H2", supported), ("H3", supported)], n_streams=39)

    assert [r["confirmatory"] for r in out] == [False, False, False]
    assert [r["verdict"] for r in out] == ["supported"] * 3


def test_partial_sessions_keep_sessions_every_condition_completed() -> None:
    complete = [make_result("s1", "A4", k, f"q{k}") for k in (1, 2, 3)]
    fb0 = make_run(
        results=complete + [make_result("s2", "A4", k, f"q{k}") for k in (1, 2, 3)],
        status={"s1/A4": "completed", "s2/A4": "completed"},
    )
    fb1 = make_run(
        results=[r.model_copy() for r in complete]
        + [make_result("s2", "A4", k, f"q{k}") for k in (1, 2)],
        status={"s1/A4": "completed", "s2/A4": "budget_exhausted"},
    )

    assert partial_sessions({"fb0": fb0, "fb1": fb1}, [FB0_A4, FB1_A4]) == {"s2": [1, 2]}


def test_merge_runs_concatenates_disjoint_runs() -> None:
    first = _scored_run({("r0", "A4"): [True]})
    second = _scored_run({("r3", "A4"): [False]})

    merged = merge_runs([first, second])

    assert sorted(merged.status) == ["r0/A4", "r3/A4"]
    assert len(merged.results) == 2


def test_merge_runs_rejects_overlapping_streams() -> None:
    run = _scored_run({("r0", "A4"): [True]})

    with pytest.raises(ValueError, match="r0/A4"):
        merge_runs([run, run])


def test_clbench_summary_compares_a4_with_a1_and_a0() -> None:
    # Per-stream accuracy A4 = 1.0, A1 = 0.5, A0 = 0.0 in both streams.
    data = _scored_run(
        {(s, "A4"): [True, True] for s in ("r0", "r1")}
        | {(s, "A1"): [True, False] for s in ("r0", "r1")}
        | {(s, "A0"): [False, False] for s in ("r0", "r1")}
    )

    summary = clbench_summary(data)

    assert summary["n_streams"] == 2
    assert summary["A4-A1"]["accuracy"] == CI(mean=0.5, low=0.5, high=0.5, n=2).model_dump()
    assert summary["A4-A0"]["score"]["mean"] == pytest.approx(1.0)


# --- summarize_followup on run directories --------------------------------------------------

_ARMS: tuple[ArmName, ...] = ("A0", "A1", "A4")
_STREAMS = ("s1", "s2")
_SESSIONS = 2
# What every A1/A4 memory says after session 1: one change record and one void-column policy.
_INJECTED = "- status was 3 in s1\n- Do not filter on void_flg\n- Delivered = 6\n"
_SOFT_DELETE = Fact(
    fact_id="softdelete.orders@s1",
    kind="soft_delete",
    subject="orders",
    value="ord_hdr.void_flg = 1",
    valid_from_session=1,
    valid_until_session=None,
    leaf=False,
)


def _write_stream(streams_root: Path, stream_id: str) -> None:
    """A two-session stream; s1 also has a soft-delete fact active from session 1."""
    stream_dir = streams_root / stream_id
    stream_dir.mkdir(parents=True)
    stream = make_stream(stream_id, sessions=_SESSIONS)
    if stream_id == "s1":
        stream = stream.model_copy(update={"facts": [*stream.facts, _SOFT_DELETE]})
    (stream_dir / "stream.json").write_text(stream.stream_json(), encoding="utf-8")
    with sqlite3.connect(stream_dir / "epoch_1.sqlite") as connection:
        connection.execute("CREATE TABLE lkp_values (cd TEXT)")
    connection.close()


def _write_job(
    layout: RunLayout,
    stream_id: str,
    arm: ArmName,
    *,
    correct: bool,
    sessions: int,
    status: RunStatus,
) -> None:
    for session in range(1, sessions + 1):
        row = make_result(stream_id, arm, session, f"q{session}", "correct" if correct else "stale")
        write_jsonl(layout.results_file(stream_id, arm, session), [row])
        path = layout.transcript_file(stream_id, arm, session)
        with TranscriptWriter(path, stream_id=stream_id, session=session) as writer:
            writer.user("?")
            writer.tool_call("query", {"sql": "select * from lkp_values"})
            writer.tool_result("query", "cd\n1")
        memory = layout.snapshot_dir(stream_id, arm, session) / "memory"
        memory.mkdir(parents=True)
        if arm in ("A1", "A4"):
            (memory / "injected.md").write_text(_INJECTED, encoding="utf-8")
    status_file = layout.status_file(stream_id, arm)
    status_file.write_text(
        json.dumps({"status": status, "completed_sessions": sessions}), encoding="utf-8"
    )


# Arms of each run label; fb0 and fb1 mirror the primary conditions.
_RUN_ARMS: dict[str, tuple[ArmName, ...]] = {
    "fb0": _ARMS,
    "fb1": _ARMS,
    "verify": _ARMS,
    "probe": ("A4",),
    "null": ("A1", "A4"),
    "sens": _ARMS,
}


def _write_followup_runs(tmp_path: Path, labels: Sequence[str]) -> dict[str, Path]:
    """Runs over s1 and s2; A4 answers right only in fb1, and fb1/A4 fails s2 after s1."""
    run_dirs: dict[str, Path] = {}
    for label in labels:
        run_dir = tmp_path / f"{label}-run"
        layout = RunLayout(run_dir)
        write_run_manifest(run_dir, arms=list(_RUN_ARMS[label]), stream_ids=list(_STREAMS))
        for stream_id in _STREAMS:
            for arm in _RUN_ARMS[label]:
                broken = (label, stream_id, arm) == ("fb1", "s2", "A4")
                _write_job(
                    layout,
                    stream_id,
                    arm,
                    correct=arm != "A4" or label == "fb1",
                    sessions=1 if broken else _SESSIONS,
                    status="failed" if broken else "completed",
                )
        run_dirs[label] = run_dir
    return run_dirs


def _summarize(tmp_path: Path, labels: Sequence[str]) -> dict[str, Any]:
    streams_root = tmp_path / "streams"
    for stream_id in _STREAMS:
        _write_stream(streams_root, stream_id)
    run_dirs = _write_followup_runs(tmp_path, labels)
    ledger = tmp_path / "ledger.sqlite"
    write_ledger(
        ledger,
        [("c1", "pilot", "fb1-run", "s1", "A4", 1, "online", 1.0, 0.5, "settled")],
    )
    return summarize_followup(run_dirs, ledger=ledger, streams_root=streams_root, clbench_dirs=[])


def test_summarize_followup_reports_primary_hypotheses_and_missing_data(tmp_path: Path) -> None:
    metrics = _summarize(tmp_path, ["fb0", "fb1"])

    primary = metrics["primary"]
    assert primary["streams"]["analyzed"] == ["s1"]
    assert primary["streams"]["excluded"] == {"failed": {"fb1/A4": 1}}
    h1, h2, h3 = primary["hypotheses"]
    assert (h1["hypothesis"], h1["mean"], h1["confirmatory"]) == ("H1", 1.0, False)
    assert (h2["mean"], h3["mean"]) == (0.0, 0.0)
    sensitivity = primary["missing_data_sensitivity"]
    assert sensitivity["streams_added"] == {"s2": [1]}
    assert sensitivity["hypotheses"]["H1"]["n"] == 2
    fb1_a4 = metrics["by_condition"]["fb1/A4"]
    assert fb1_a4["unfiltered_ref_query_rate"] == 1.0
    assert fb1_a4["cost_per_correct_usd"] == pytest.approx(0.5 / 2)
    assert metrics["exploratory"]["clbench"] is None
    assert metrics["exploratory"]["mechanism"] is None


def test_summarize_followup_reads_memory_snapshots_of_auxiliary_runs(tmp_path: Path) -> None:
    metrics = _summarize(tmp_path, ["fb0", "fb1", "verify", "probe", "null", "sens"])

    exploratory = metrics["exploratory"]
    assert exploratory["null"]["null"]["null/A4"]["change_records_per_snapshot"] == 1.0
    assert exploratory["null"]["fb0"]["fb0/A1"]["change_records_per_snapshot"] == 1.0
    # Only s1 has a soft-delete fact, so only its snapshots count void-column policies.
    void_lines = exploratory["probe"]["void_policy_lines"]
    assert void_lines["probe/A4"] == 1.0
    assert void_lines["difference"]["n"] == 1
    assert exploratory["mechanism"]["interaction_A4_vs_A0"]["n_streams"] == 2
    # sens/A4 and fb0/A4 both answer nothing right, sens/A1 and fb0/A1 everything.
    assert exploratory["sensitivity"]["A4-A1"]["sens"]["mean"] == -1.0
    assert exploratory["sensitivity"]["A4-A1"]["fb0"]["mean"] == -1.0


def test_summarize_followup_requires_both_feedback_runs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fb1"):
        summarize_followup(
            {"fb0": tmp_path}, ledger=tmp_path / "l.sqlite", streams_root=tmp_path, clbench_dirs=[]
        )
