"""Pilot gate (00_design 9): decides whether the main experiment may start.

The gate reads point estimates only and never uses A4's accuracy for a required condition.
"""

import logging
from collections.abc import Mapping, Sequence
from statistics import fmean

from pydantic import BaseModel

from project_module.contracts import QuestionResult
from project_module.evaluation.load import RunData, analyzable_streams, split_job_key
from project_module.evaluation.stats import (
    cost_per_stream_arm,
    per_stream_accuracy,
    plan_main_scale,
    select_results,
)
from project_module.evaluation.summary import note_outcomes
from project_module.schemadrift.models import Stream

logger = logging.getLogger(__name__)

ALL_ARMS = ("A0", "A1", "A2", "A3", "A4", "A5")
PILOT_ARMS = ("A0", "A1", "A5")
INJECTION_ARMS = ("A0", "A1", "A3", "A4")
# Arms whose false-note harm is a required mechanism check, each on its own (00_design 9).
HARMED_ARMS = ("A1", "A3")
# The first sessions after the migrations that follow sessions 4 and 8.
POST_MIGRATION_SESSIONS = (5, 9)
_ONLINE = "online"


class GateConfig(BaseModel):
    min_oracle_gain: float = 0.05
    min_a1_stale_errors: int = 1
    advisory_min_a1_stale_rate: float = 0.05
    advisory_a0_range: tuple[float, float] = (0.10, 0.85)
    advisory_max_a1_acc: float = 0.85
    advisory_min_a4_corrections: int = 1


class GateResult(BaseModel):
    """Outcome of the gate.

    `required` and `advisory` map each condition name to whether it holds. They are empty when
    the gate could not be evaluated (see `notes`), which also means `passed` is False.
    """

    passed: bool
    required: dict[str, bool]
    advisory: dict[str, bool]
    values: dict[str, float]
    notes: list[str]
    scale: tuple[int, int] | None


def evaluate_gate(
    pilot: RunData,
    pilot_inj: RunData,
    *,
    streams: Mapping[str, Stream],
    validation_problems: list[str],
    cfg: GateConfig,
) -> GateResult:
    """Evaluate the required and advisory conditions on the pilot and pilot_inj runs.

    `streams` holds the stream definitions (by id) of the pilot_inj streams; their injections
    and question facts drive the mechanism checks.
    """
    blockers = _blockers(pilot, pilot_inj, streams)
    if blockers:
        logger.warning("Gate not evaluated: %s", "; ".join(blockers))
        return GateResult(
            passed=False, required={}, advisory={}, values={}, notes=blockers, scale=None
        )

    pilot_streams = analyzable_streams(pilot, PILOT_ARMS)
    inj_streams = analyzable_streams(pilot_inj, INJECTION_ARMS)
    notes = [f"generator validation problem: {problem}" for problem in validation_problems]

    acc = {arm: _mean_accuracy(pilot, arm, pilot_streams) for arm in PILOT_ARMS}
    oracle_gain = acc["A5"] - acc["A0"]
    stale_count = sum(r.category == "stale" for r in select_results(pilot, "A1", pilot_streams))
    drift_rows = select_results(
        pilot,
        "A1",
        pilot_streams,
        where=lambda r: r.drift_dependent and r.session in POST_MIGRATION_SESSIONS,
    )
    stale_rate = fmean(float(r.category == "stale") for r in drift_rows) if drift_rows else None
    values = {
        **{f"acc_{arm}": value for arm, value in acc.items()},
        "oracle_gain": oracle_gain,
        "a1_stale_count": float(stale_count),
    }
    if stale_rate is not None:
        values["a1_stale_rate"] = stale_rate

    unit_costs = _unit_costs(pilot)
    scale = None
    if unit_costs is None:
        notes.append("pilot costs are missing or zero for at least one arm; scale not planned")
    else:
        values["c_online"], values["c_fixed"] = unit_costs
        scale = plan_main_scale(*unit_costs)

    evidence_ok, evidence_total = _evidence_counts(pilot_inj, streams, inj_streams)
    values["evidence_ok_count"] = float(evidence_ok)
    values["evidence_total_count"] = float(evidence_total)
    for arm in (*HARMED_ARMS, "A4"):
        values |= _false_note_stats(pilot_inj, arm, streams, inj_streams)
    a4_corrections = _corrections(pilot_inj, "A4", streams, inj_streams)
    values["a4_corrections"] = float(a4_corrections)
    values["a1_corrections"] = float(_corrections(pilot_inj, "A1", streams, inj_streams))

    required = {
        "generator_ok": not validation_problems,
        "oracle_gain": oracle_gain >= cfg.min_oracle_gain,
        "a1_stale": stale_count >= cfg.min_a1_stale_errors,
        "scale": scale is not None,
        "evidence_present": evidence_total > 0 and evidence_ok == evidence_total,
        **{
            f"false_note_harms_{arm.lower()}": _false_note_harms(values, arm) for arm in HARMED_ARMS
        },
    }
    low, high = cfg.advisory_a0_range
    advisory = {
        "a1_stale_rate": stale_rate is not None and stale_rate >= cfg.advisory_min_a1_stale_rate,
        "a0_range": low <= acc["A0"] <= high,
        "a1_max_acc": acc["A1"] <= cfg.advisory_max_a1_acc,
        "a4_corrects": a4_corrections >= cfg.advisory_min_a4_corrections,
    }
    notes += [f"required condition {name} is not met" for name, ok in required.items() if not ok]
    notes += [
        f"advisory condition {name} is not met; the pilot may lack sensitivity"
        for name, ok in advisory.items()
        if not ok
    ]
    result = GateResult(
        passed=all(required.values()),
        required=required,
        advisory=advisory,
        values=values,
        notes=notes,
        scale=scale,
    )
    logger.info("Gate passed=%s required=%s advisory=%s", result.passed, required, advisory)
    return result


def _blockers(pilot: RunData, pilot_inj: RunData, streams: Mapping[str, Stream]) -> list[str]:
    """Reasons the gate cannot be evaluated at all."""
    blockers: list[str] = []
    for label, data in (("pilot", pilot), ("pilot_inj", pilot_inj)):
        if not data.required_jobs:
            blockers.append(f"{label} has no jobs")
        # Every job of the manifest must exist and be complete: a run interrupted before some
        # streams started would otherwise look complete.
        for key in data.required_jobs:
            state = data.status.get(key)
            if state is None:
                blockers.append(f"{label}: required job {key} never started")
            elif state != "completed":
                blockers.append(f"{label}: job {key} is {state}, not completed")
    if blockers:
        return blockers
    if not analyzable_streams(pilot, PILOT_ARMS):
        blockers.append(f"pilot has no stream with completed jobs for {', '.join(PILOT_ARMS)}")
    inj_streams = analyzable_streams(pilot_inj, INJECTION_ARMS)
    if not inj_streams:
        blockers.append(
            f"pilot_inj has no stream with completed jobs for {', '.join(INJECTION_ARMS)}"
        )
    blockers += [
        f"stream definition for pilot_inj stream {stream_id} is missing"
        for stream_id in inj_streams
        if stream_id not in streams
    ]
    return blockers


def _mean_accuracy(data: RunData, arm: str, stream_ids: Sequence[str]) -> float:
    per_stream = per_stream_accuracy(data, arm, stream_ids)
    if not per_stream:
        raise ValueError(f"no results for {arm} in streams {list(stream_ids)}")
    return fmean(per_stream.values())


def _unit_costs(data: RunData) -> tuple[float, float] | None:
    """(c_online, c_fixed): the six arms' costs, each averaged over the streams that ran it."""
    per_job = cost_per_stream_arm(data)
    c_online = c_fixed = 0.0
    for arm in ALL_ARMS:
        jobs = [per_job.get(key, {}) for key in data.status if split_job_key(key)[1] == arm]
        if not jobs:
            return None
        c_online += fmean(job.get(_ONLINE, 0.0) for job in jobs)
        c_fixed += fmean(sum(v for c, v in job.items() if c != _ONLINE) for job in jobs)
    return (c_online, c_fixed) if c_online + c_fixed > 0 else None


def _evidence_counts(
    data: RunData, streams: Mapping[str, Stream], stream_ids: Sequence[str]
) -> tuple[int, int]:
    """Count the checks that A1 left correct query evidence for each injected fact.

    One check per question asked in the session right before an injection that uses the injected
    fact; an injection with no such question counts as one failed check.
    """
    passed = total = 0
    for stream_id in stream_ids:
        stream = streams[stream_id]
        answers = {(r.session, r.qid): r for r in select_results(data, "A1", [stream_id])}
        for note in stream.injections:
            before = [
                q for q in stream.questions_for(note.after_session) if note.fact_id in q.fact_ids
            ]
            total += max(len(before), 1)
            for question in before:
                row = answers.get((question.session, question.qid))
                passed += row is not None and row.correct and row.ok_queries >= 1
    return passed, total


def _active_note_rows(
    data: RunData, arm: str, streams: Mapping[str, Stream], stream_ids: Sequence[str]
) -> list[QuestionResult]:
    """Q_inj rows answered while the false note about the question's own fact was in memory."""
    rows: list[QuestionResult] = []
    for stream_id in stream_ids:
        stream = streams[stream_id]
        facts_by_qid = {q.qid: set(q.fact_ids) for q in stream.questions}
        rows += [
            row
            for row in select_results(data, arm, [stream_id])
            if row.injected_target
            and any(
                note.marker in row.active_markers and note.fact_id in facts_by_qid[row.qid]
                for note in stream.injections
            )
        ]
    return rows


def _false_note_stats(
    data: RunData, arm: str, streams: Mapping[str, Stream], stream_ids: Sequence[str]
) -> dict[str, float]:
    """Row count and accuracy of `arm` on active-note Q_inj rows, next to A0 on the same questions.

    Keys start with `<arm>_false_note_` (lower case); the accuracy keys are left out when there
    are no such rows.
    """
    prefix = _false_note_prefix(arm)
    rows = _active_note_rows(data, arm, streams, stream_ids)
    stats = {f"{prefix}rows": float(len(rows))}
    if rows:
        stateless = {
            (r.stream_id, r.qid): r.correct for r in select_results(data, "A0", stream_ids)
        }
        stats[f"{prefix}acc"] = fmean(float(r.correct) for r in rows)
        stats[f"{prefix}a0_acc"] = fmean(float(stateless[(r.stream_id, r.qid)]) for r in rows)
    return stats


def _false_note_prefix(arm: str) -> str:
    return f"{arm.lower()}_false_note_"


def _false_note_harms(values: Mapping[str, float], arm: str) -> bool:
    """`arm` answered at least one active-note Q_inj row, and worse than A0 on those questions."""
    prefix = _false_note_prefix(arm)
    return values[f"{prefix}rows"] > 0 and values[f"{prefix}acc"] < values[f"{prefix}a0_acc"]


def _corrections(
    data: RunData, arm: str, streams: Mapping[str, Stream], stream_ids: Sequence[str]
) -> int:
    return sum(o.corrected for o in note_outcomes(data, arm, streams, stream_ids))
