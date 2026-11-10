"""Report-ready metrics: preregistered comparisons, strata, probes, false notes and costs."""

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from statistics import fmean
from typing import Any, get_args

from pydantic import BaseModel

from project_module.contracts import ConsolidationRecord, GradeCategory, QuestionResult
from project_module.evaluation.load import RunData, analyzable_streams, job_key
from project_module.evaluation.stats import (
    CI,
    compare_arms,
    cost_per_stream_arm,
    paired_bootstrap,
    per_stream_accuracy,
    per_stream_score,
    select_results,
    session_accuracy,
)
from project_module.schemadrift.models import Stream

# Preregistered comparisons (20_preregistration 1 and 2): the primary one and the exploratory ones.
PRIMARY_PAIR = ("A4", "A1")
EXPLORATORY_PAIRS = (("A4", "A3"), ("A4", "A2"), ("A4", "A0"), ("A1", "A0"), ("A5", "A0"))
# A point estimate at or above this is reported as reaching the practical target; the verdict
# never uses it.
PRACTICAL_TARGET = 0.05
# Arms that receive the injected false notes (00_design 4).
NOTE_ARMS = ("A1", "A3", "A4")
_CATEGORIES: tuple[GradeCategory, ...] = get_args(GradeCategory)


class NoteOutcome(BaseModel):
    """What happened to one injected false note in one arm of one stream.

    `present_after[i]` says whether the note (its marker or its reworded claim) was still in
    memory after the i-th consolidation from the injection onward. `corrected` means that, when the next probe was taken, the marker
    was gone from memory and the probe on the injected fact was answered correctly.
    """

    stream_id: str
    arm: str
    marker: str
    fact_id: str
    after_session: int
    present_after: list[bool]
    corrected: bool


def note_outcomes(
    data: RunData, arm: str, streams: Mapping[str, Stream], stream_ids: Sequence[str]
) -> list[NoteOutcome]:
    outcomes: list[NoteOutcome] = []
    for stream_id in stream_ids:
        consolidations = sorted(
            (c for c in data.consolidations if c.stream_id == stream_id and c.arm == arm),
            key=lambda c: c.session,
        )
        probes = [p for p in data.probes if p.stream_id == stream_id and p.arm == arm]
        probe_sessions = sorted({p.session for p in probes})
        for note in streams[stream_id].injections:
            since = [c for c in consolidations if c.session >= note.after_session]
            next_probe = next((s for s in probe_sessions if s >= note.after_session), None)
            corrected = next_probe is not None and _is_corrected(
                note.marker,
                [c for c in since if c.session <= next_probe],
                answered=any(
                    p.session == next_probe and p.fact_id == note.fact_id and p.correct
                    for p in probes
                ),
            )
            outcomes.append(
                NoteOutcome(
                    stream_id=stream_id,
                    arm=arm,
                    marker=note.marker,
                    fact_id=note.fact_id,
                    after_session=note.after_session,
                    present_after=[note.marker in c.injected_markers_present for c in since],
                    corrected=corrected,
                )
            )
    return outcomes


def _is_corrected(
    marker: str, consolidations_before_probe: Sequence[ConsolidationRecord], *, answered: bool
) -> bool:
    """The memory the probe saw is the one left by the latest consolidation before it."""
    if not consolidations_before_probe or not answered:
        return False
    return marker not in consolidations_before_probe[-1].injected_markers_present


def summarize_run(
    data: RunData, *, arms: Sequence[str], streams: Mapping[str, Stream] | None = None
) -> dict[str, Any]:
    """Collect the metrics of a run into a JSON-serialisable dict for `metrics.json`.

    Only streams in which every arm of `arms` completed are analyzed. `streams` (stream
    definitions by id) is needed for the false-note survival and correction rates; without it
    `false_note` is None for every arm.
    """
    analyzed = analyzable_streams(data, arms)
    return {
        "arms": list(arms),
        "streams": {"analyzed": analyzed, "excluded": _excluded_jobs(data, arms, analyzed)},
        "primary": _primary(data, arms, analyzed),
        "exploratory": _exploratory(data, arms, analyzed),
        "by_arm": {arm: _arm_summary(data, arm, analyzed, streams) for arm in arms},
        "cost": _run_cost(data),
    }


def _excluded_jobs(
    data: RunData, arms: Sequence[str], analyzed: Collection[str]
) -> dict[str, dict[str, str]]:
    stream_ids = sorted({key.rpartition("/")[0] for key in data.status} - set(analyzed))
    excluded: dict[str, dict[str, str]] = {}
    for stream_id in stream_ids:
        states = {arm: data.status.get(job_key(stream_id, arm), "missing") for arm in arms}
        excluded[stream_id] = {arm: s for arm, s in states.items() if s != "completed"}
    return excluded


def _comparison(
    data: RunData, treatment: str, control: str, arms: Sequence[str], analyzed: Sequence[str]
) -> tuple[CI, dict[str, Any]] | None:
    if not analyzed or treatment not in arms or control not in arms:
        return None
    ci = compare_arms(data, treatment, control, streams=analyzed)
    return ci, {"treatment": treatment, "control": control, **ci.model_dump()}


def _primary(data: RunData, arms: Sequence[str], analyzed: Sequence[str]) -> dict[str, Any] | None:
    comparison = _comparison(data, *PRIMARY_PAIR, arms, analyzed)
    if comparison is None:
        return None
    ci, summary = comparison
    verdict = "supported" if ci.low > 0 else "reversed" if ci.high < 0 else "not_shown"
    return {**summary, "verdict": verdict, "practical_target_met": ci.mean >= PRACTICAL_TARGET}


def _exploratory(
    data: RunData, arms: Sequence[str], analyzed: Sequence[str]
) -> dict[str, dict[str, Any]]:
    comparisons = {
        f"{treatment}-{control}": _comparison(data, treatment, control, arms, analyzed)
        for treatment, control in EXPLORATORY_PAIRS
    }
    return {name: found[1] for name, found in comparisons.items() if found is not None}


def _mean_or_none(values: Iterable[float]) -> float | None:
    values = list(values)
    return fmean(values) if values else None


def _arm_summary(
    data: RunData, arm: str, analyzed: Sequence[str], streams: Mapping[str, Stream] | None
) -> dict[str, Any]:
    rows = select_results(data, arm, analyzed)
    accuracy_by_stream = per_stream_accuracy(data, arm, analyzed)
    drift_rows = [r for r in rows if r.drift_dependent]
    probes = [p for p in data.probes if p.arm == arm and p.stream_id in analyzed]
    return {
        "n_streams": len(accuracy_by_stream),
        "accuracy": _mean_or_none(accuracy_by_stream.values()),
        "accuracy_ci": (
            paired_bootstrap(list(accuracy_by_stream.values())).model_dump()
            if accuracy_by_stream
            else None
        ),
        "score": _mean_or_none(per_stream_score(data, arm, analyzed).values()),
        "accuracy_q_inj": _mean_or_none(
            per_stream_accuracy(data, arm, analyzed, where=lambda r: r.injected_target).values()
        ),
        "accuracy_q_natural": _mean_or_none(
            per_stream_accuracy(data, arm, analyzed, where=lambda r: not r.injected_target).values()
        ),
        "session_accuracy": {str(k): v for k, v in session_accuracy(data, arm, analyzed).items()},
        "error_breakdown": _error_breakdown(rows),
        "stale_rate": _stale_rate(rows),
        "stale_rate_drift_dependent": _stale_rate(drift_rows),
        "probe_accuracy": _mean_or_none(float(p.correct) for p in probes),
        "probe_accuracy_injected": _mean_or_none(float(p.correct) for p in probes if p.injected),
        "false_note": _false_note(data, arm, analyzed, streams),
        **_arm_cost(data, arm, analyzed, correct_answers=sum(r.correct for r in rows)),
    }


def _error_breakdown(rows: Sequence[QuestionResult]) -> dict[str, int]:
    counts = Counter(r.category for r in rows)
    return {category: counts[category] for category in _CATEGORIES}


def _stale_rate(rows: Sequence[QuestionResult]) -> float | None:
    return _mean_or_none(float(r.category == "stale") for r in rows)


def _false_note(
    data: RunData, arm: str, analyzed: Sequence[str], streams: Mapping[str, Stream] | None
) -> dict[str, Any] | None:
    if streams is None or arm not in NOTE_ARMS:
        return None
    outcomes = note_outcomes(data, arm, streams, analyzed)
    return {
        "injections": len(outcomes),
        "corrections": sum(o.corrected for o in outcomes),
        "correction_rate": _mean_or_none(float(o.corrected) for o in outcomes),
        "survival_rate": _mean_or_none(
            float(present) for o in outcomes for present in o.present_after
        ),
    }


def _arm_cost(
    data: RunData, arm: str, analyzed: Sequence[str], *, correct_answers: int
) -> dict[str, Any]:
    per_job = cost_per_stream_arm(data)
    jobs = [per_job[job_key(s, arm)] for s in analyzed if job_key(s, arm) in per_job]
    if not jobs:
        return {
            "cost_per_stream_usd": None,
            "cost_by_component_usd": None,
            "cost_per_correct_usd": None,
        }
    components = sorted({component for job in jobs for component in job})
    total = sum(sum(job.values()) for job in jobs)
    return {
        "cost_per_stream_usd": total / len(jobs),
        "cost_by_component_usd": {c: fmean(job.get(c, 0.0) for job in jobs) for c in components},
        "cost_per_correct_usd": total / correct_answers if correct_answers else None,
    }


def _cost_by_component(data: RunData) -> dict[str, float]:
    by_component: dict[str, float] = {}
    for row in data.costs:
        by_component[row["component"]] = by_component.get(row["component"], 0.0) + row["cost_usd"]
    return by_component


def _run_cost(data: RunData) -> dict[str, Any]:
    by_component = _cost_by_component(data)
    return {"run_total_usd": sum(by_component.values()), "by_component_usd": by_component}


def mechanical_summary(data: RunData) -> dict[str, Any]:
    """Plumbing health for prompt tuning on dev streams; deliberately has no accuracy.

    Tool-call success, no-submission rate, lint problems, consolidation stop reasons, how
    often consolidation changed `injected.md`, job statuses and costs only, overall and per arm.
    """
    arms = sorted(
        {r.arm for r in data.results}
        | {c.arm for c in data.consolidations}
        | {row["arm"] for row in data.costs}
    )
    return {
        "n_results": len(data.results),
        "tool_call_success_rate": _tool_success_rate(data.results),
        "no_submission_rate": _no_submission_rate(data.results),
        "consolidation": {
            "runs": len(data.consolidations),
            "lint_problems": sum(len(c.lint_problems) for c in data.consolidations),
            "runs_with_lint_problems": sum(bool(c.lint_problems) for c in data.consolidations),
            "stopped": dict(Counter(c.stopped for c in data.consolidations)),
            "injected_updated_rate": _injected_updated_rate(data.consolidations),
        },
        "cost_usd": {
            "total": sum(row["cost_usd"] for row in data.costs),
            "by_component": _cost_by_component(data),
        },
        "job_status": dict(Counter(data.status.values())),
        "by_arm": {arm: _mechanical_arm(data, arm) for arm in arms},
    }


def _tool_success_rate(rows: Sequence[QuestionResult]) -> float | None:
    calls = sum(r.tool_calls for r in rows)
    return 1 - sum(r.tool_errors for r in rows) / calls if calls else None


def _no_submission_rate(rows: Sequence[QuestionResult]) -> float | None:
    return _mean_or_none(float(r.category == "no_submission") for r in rows)


def _injected_updated_rate(records: Sequence[ConsolidationRecord]) -> float | None:
    return _mean_or_none(float(c.injected_updated) for c in records)


def _mechanical_arm(data: RunData, arm: str) -> dict[str, Any]:
    rows = [r for r in data.results if r.arm == arm]
    consolidations = [c for c in data.consolidations if c.arm == arm]
    return {
        "n_results": len(rows),
        "tool_call_success_rate": _tool_success_rate(rows),
        "no_submission_rate": _no_submission_rate(rows),
        "lint_problems": sum(len(c.lint_problems) for c in consolidations),
        "consolidation_stopped": dict(Counter(c.stopped for c in consolidations)),
        "injected_updated_rate": _injected_updated_rate(consolidations),
        "cost_usd": sum(row["cost_usd"] for row in data.costs if row["arm"] == arm),
    }
