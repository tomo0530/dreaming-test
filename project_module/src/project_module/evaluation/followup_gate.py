"""Follow-up pilot gate G0-G7 (docs/followup/00_design.md 7).

The gate checks only that the treatments worked mechanically; no accuracy, arm difference or
oracle gap enters any condition. Rates pool every pilot run that holds the arm.
"""

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from project_module.config import ExperimentConfig
from project_module.contracts import QuestionResult
from project_module.evaluation.exploration import question_blocks
from project_module.evaluation.followup import condition_label
from project_module.evaluation.load import RunData, load_run, split_job_key
from project_module.evaluation.stats import cost_per_stream_arm
from project_module.layout import RunLayout
from project_module.schemadrift.generator import STREAM_FILE, load_stream
from project_module.schemadrift.validate import validate_stream

logger = logging.getLogger(__name__)

PILOT_LABELS = ("fb0", "fb1", "verify", "probe", "null")
NOTE_ARMS = ("A1", "A4")
NOTE_RATE_MIN = 0.90
NOTES_READ_RATE_MIN = 0.90
RETRIEVAL_RATE_MIN = 0.95
NO_SUBMISSION_MAX = 0.10
SQL_PROBE_RATE_MIN = 0.50
# Covers uncached prompts, retries and reservations not settled yet (00_design 7, G6).
COST_SAFETY = 1.3
_FEEDBACK_LABEL = "EVENT feedback:"
STREAM_NOT_GENERATED = "stream not generated"


def _rate(flags: Sequence[bool]) -> float | None:
    return fmean(float(flag) for flag in flags) if flags else None


def _at_least(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _g0_completion(runs: Mapping[str, RunData]) -> dict[str, Any]:
    """Every job each pilot manifest asks for has completed."""
    unfinished = {
        f"{label}:{key}": data.status.get(key, "not_started")
        for label, data in runs.items()
        for key in data.required_jobs
        if data.status.get(key) != "completed"
    }
    return {"passed": not unfinished, "unfinished_jobs": unfinished}


def _g1_validation(problems: Mapping[str, list[str]]) -> dict[str, Any]:
    failing = {stream_id: found for stream_id, found in sorted(problems.items()) if found}
    return {"passed": not failing, "streams_checked": len(problems), "problems": failing}


def _feedback_lines(block: Sequence[str]) -> int:
    return sum(record.startswith(_FEEDBACK_LABEL) for record in block)


def _session_blocks(run_dir: Path, data: RunData) -> list[tuple[int, list[list[str]]]]:
    """(graded questions, question blocks) of every session transcript with results."""
    layout = RunLayout(run_dir)
    graded: dict[tuple[str, str, int], int] = defaultdict(int)
    for row in data.results:
        graded[(row.stream_id, row.arm, row.session)] += 1
    sessions: list[tuple[int, list[list[str]]]] = []
    for (stream_id, arm, session), count in sorted(graded.items()):
        path = layout.transcript_file(stream_id, arm, session)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        sessions.append((count, question_blocks(text)))
    return sessions


def _g2_feedback(run_dirs: Mapping[str, Path], runs: Mapping[str, RunData]) -> dict[str, Any]:
    """fb1 has exactly one feedback line per graded question; fb0 has none at all."""
    graded = with_feedback = 0
    aligned = True
    for count, blocks in _session_blocks(run_dirs["fb1"], runs["fb1"]):
        graded += count
        with_feedback += sum(_feedback_lines(block) == 1 for block in blocks)
        aligned = aligned and len(blocks) == count
    fb0_lines = sum(
        _feedback_lines(block)
        for _, blocks in _session_blocks(run_dirs["fb0"], runs["fb0"])
        for block in blocks
    )
    passed = aligned and graded > 0 and with_feedback == graded and fb0_lines == 0
    return {
        "passed": passed,
        "fb1_graded_questions": graded,
        "fb1_questions_with_feedback": with_feedback,
        "fb1_transcripts_match_results": aligned,
        "fb0_feedback_lines": fb0_lines,
    }


def _rows(runs: Mapping[str, RunData], arm: str) -> list[QuestionResult]:
    return [r for data in runs.values() for r in data.results if r.arm == arm]


def _notes_read_flags(runs: Mapping[str, RunData], arm: str) -> list[bool]:
    """For each consolidation of a session that wrote a note, whether it read a live note."""
    flags: list[bool] = []
    for data in runs.values():
        noted = {(r.stream_id, r.session) for r in data.results if r.arm == arm and r.note_written}
        flags += [
            c.notes_files_read >= 1
            for c in data.consolidations
            if c.arm == arm and (c.stream_id, c.session) in noted
        ]
    return flags


def _g3_notes(runs: Mapping[str, RunData]) -> dict[str, Any]:
    """A1 and A4 each write a note for 90% of questions and read notes in 90% of consolidations."""
    report: dict[str, Any] = {}
    for arm in NOTE_ARMS:
        rows = _rows(runs, arm)
        read = _notes_read_flags(runs, arm)
        note_rate = _rate([r.note_written for r in rows])
        notes_read_rate = _rate(read)
        report[arm] = {
            "questions": len(rows),
            "note_rate": note_rate,
            "consolidations_after_notes": len(read),
            "notes_read_rate": notes_read_rate,
            "passed": _at_least(note_rate, NOTE_RATE_MIN)
            and _at_least(notes_read_rate, NOTES_READ_RATE_MIN),
        }
    return {"passed": all(report[arm]["passed"] for arm in NOTE_ARMS), **report}


def _g4_retrieval(runs: Mapping[str, RunData]) -> dict[str, Any]:
    """A2 got injected excerpts for 95% of the questions from session 2 on."""
    rows = [r for r in _rows(runs, "A2") if r.session >= 2]
    rate = _rate([r.retrieved_tokens > 0 for r in rows])
    return {
        "passed": _at_least(rate, RETRIEVAL_RATE_MIN),
        "questions": len(rows),
        "retrieval_rate": rate,
    }


def _g5_no_submission(runs: Mapping[str, RunData]) -> dict[str, Any]:
    """Every arm of every pilot run leaves at most 10% of its questions unanswered."""
    rates: dict[str, float | None] = {}
    for label, data in runs.items():
        for arm in sorted({r.arm for r in data.results}):
            rows = [r for r in data.results if r.arm == arm]
            rates[f"{label}/{arm}"] = _rate([r.category == "no_submission" for r in rows])
    passed = bool(rates) and all(
        rate is not None and rate <= NO_SUBMISSION_MAX for rate in rates.values()
    )
    return {"passed": passed, "no_submission_rate": rates}


def _unit_costs(runs: Mapping[str, RunData]) -> dict[str, float]:
    """Mean ledger cost per job (stream) of each pilot condition that has any cost."""
    units: dict[str, float] = {}
    for label, data in runs.items():
        per_job = cost_per_stream_arm(data)
        by_arm: dict[str, list[float]] = defaultdict(list)
        for key in data.required_jobs:
            by_arm[split_job_key(key)[1]].append(sum(per_job.get(key, {}).values()))
        for arm, costs in by_arm.items():
            if fmean(costs) > 0:
                units[condition_label((label, arm))] = fmean(costs)
    return units


def _g6_cost(
    runs: Mapping[str, RunData],
    main_jobs: Mapping[str, Mapping[str, int]],
    phase_caps: Mapping[str, float],
) -> dict[str, Any]:
    """Pilot unit cost x main jobs x 1.3 stays within each main phase's cap."""
    missing_caps = sorted(set(main_jobs) - set(phase_caps))
    if missing_caps:
        raise ValueError(f"no phase cap for the main phases {missing_caps}")
    units = _unit_costs(runs)
    unpriced = sorted({cond for jobs in main_jobs.values() for cond in jobs} - set(units))
    projected = {
        phase: COST_SAFETY * sum(units.get(cond, 0.0) * count for cond, count in jobs.items())
        for phase, jobs in main_jobs.items()
    }
    within = all(projected[phase] <= phase_caps[phase] for phase in projected)
    return {
        "passed": bool(main_jobs) and not unpriced and within,
        "unit_cost_usd": units,
        "unpriced": unpriced,
        "projected_usd": projected,
        "phase_caps_usd": dict(phase_caps),
    }


def _g7_sql_probe(runs: Mapping[str, RunData]) -> dict[str, Any]:
    """Half of the probe run's A4 consolidations ran at least one successful query."""
    flags = [c.sql_queries >= 1 for c in runs["probe"].consolidations if c.arm == "A4"]
    rate = _rate(flags)
    return {
        "passed": _at_least(rate, SQL_PROBE_RATE_MIN),
        "consolidations": len(flags),
        "query_rate": rate,
    }


def evaluate_followup_gate(
    run_dirs: Mapping[str, Path],
    *,
    ledger: Path,
    validation_problems: Mapping[str, list[str]],
    main_jobs: Mapping[str, Mapping[str, int]],
    phase_caps: Mapping[str, float],
) -> dict[str, Any]:
    """Evaluate G0-G7 on the five pilot runs.

    Parameters
    ----------
    run_dirs
        Pilot run directories keyed `fb0`, `fb1`, `verify`, `probe` and `null`.
    ledger
        The shared cost ledger.
    validation_problems
        `validate_stream` problems of every pilot and main stream, by stream id.
    main_jobs
        Main jobs per phase (`fu_main`, `fu_aux`; never `fu_sens`) and condition (`fb0/A4`);
        a condition's label must match the pilot run that prices it.
    phase_caps
        Cap in USD of every phase in `main_jobs`.

    Returns
    -------
    dict
        `passed`, the thresholds, and `conditions` mapping G0..G7 to their values and `passed`.

    Raises
    ------
    ValueError
        If `run_dirs` does not hold exactly the five pilot labels, or a phase has no cap.
    """
    if set(run_dirs) != set(PILOT_LABELS):
        raise ValueError(
            f"the gate needs the pilot runs {list(PILOT_LABELS)}, got {sorted(run_dirs)}"
        )
    runs = {label: load_run(run_dirs[label], ledger) for label in PILOT_LABELS}
    conditions = {
        "G0": _g0_completion(runs),
        "G1": _g1_validation(validation_problems),
        "G2": _g2_feedback(run_dirs, runs),
        "G3": _g3_notes(runs),
        "G4": _g4_retrieval(runs),
        "G5": _g5_no_submission(runs),
        "G6": _g6_cost(runs, main_jobs, phase_caps),
        "G7": _g7_sql_probe(runs),
    }
    failed = [name for name, condition in conditions.items() if not condition["passed"]]
    passed = not failed
    logger.info("Follow-up gate passed=%s failed=%s", passed, failed)
    return {
        "passed": passed,
        "failed": failed,
        "thresholds": {
            "note_rate_min": NOTE_RATE_MIN,
            "notes_read_rate_min": NOTES_READ_RATE_MIN,
            "retrieval_rate_min": RETRIEVAL_RATE_MIN,
            "no_submission_max": NO_SUBMISSION_MAX,
            "sql_probe_rate_min": SQL_PROBE_RATE_MIN,
            "cost_safety": COST_SAFETY,
        },
        "conditions": conditions,
    }


def count_main_jobs(
    configs: Mapping[str, ExperimentConfig],
) -> tuple[dict[str, dict[str, int]], dict[str, float]]:
    """Main jobs per phase and condition, and each phase's cap, from the main configurations.

    `configs` maps the run label (the label of the pilot run with the same settings) to the
    main configuration.

    Raises
    ------
    ValueError
        If configurations of one phase disagree on `phase_cap_usd`.
    """
    jobs: dict[str, dict[str, int]] = {}
    caps: dict[str, float] = {}
    for label, config in configs.items():
        cap = caps.setdefault(config.phase, config.budgets.phase_cap_usd)
        if cap != config.budgets.phase_cap_usd:
            raise ValueError(
                f"main configurations of phase {config.phase} disagree on phase_cap_usd: "
                f"{cap} and {config.budgets.phase_cap_usd}"
            )
        counts = jobs.setdefault(config.phase, {})
        for stream_id in config.stream_ids:
            for arm in config.arms_for(stream_id):
                key = condition_label((label, arm))
                counts[key] = counts.get(key, 0) + 1
    return jobs, caps


def stream_validation_problems(
    stream_ids: Sequence[str], streams_root: Path
) -> dict[str, list[str]]:
    """`validate_stream` problems of each stream; a stream that was never generated is one."""
    problems: dict[str, list[str]] = {}
    for stream_id in stream_ids:
        stream_dir = streams_root / stream_id
        if not (stream_dir / STREAM_FILE).is_file():
            problems[stream_id] = [STREAM_NOT_GENERATED]
            continue
        problems[stream_id] = validate_stream(load_stream(stream_dir))
    return problems
