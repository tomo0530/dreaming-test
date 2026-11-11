"""Multi-run analysis of the follow-up study (docs/followup/20_preregistration.md 2-5).

A condition is a (run label, arm) pair such as ("fb1", "A4"), written `fb1/A4`. Runs are read
once into `RunData` keyed by label, and every comparison pairs the per-stream scores of two or
four conditions over the streams in which all of them completed.
"""

import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from project_module.contracts import QuestionResult
from project_module.evaluation.exploration import (
    QuestionFeatures,
    count_change_records,
    count_void_policy_lines,
    question_drift_kinds,
    question_features,
    reference_table,
    void_columns,
)
from project_module.evaluation.load import (
    RunData,
    analyzable_streams,
    job_key,
    load_run,
    split_job_key,
)
from project_module.evaluation.stats import (
    CI,
    ResultFilter,
    paired_bootstrap,
    per_stream_accuracy,
    per_stream_score,
)
from project_module.evaluation.summary import (
    PRACTICAL_TARGET,
    arm_cost,
    error_breakdown,
    mean_or_none,
    no_submission_rate,
)
from project_module.layout import RunLayout
from project_module.manifest import load_manifest
from project_module.schemadrift.generator import load_stream
from project_module.schemadrift.models import Stream

logger = logging.getLogger(__name__)

Condition = tuple[str, str]
Verdict = Literal["supported", "not_shown", "reversed"]

FOLLOWUP_BOOTSTRAP_SEED = 20261111
BOOTSTRAP_RESAMPLES = 10_000
# Confirmatory verdicts need every main stream completed in all six primary conditions.
REQUIRED_STREAMS = 40
NOT_STARTED = "not_started"

FB0, FB1 = "fb0", "fb1"
PRIMARY_CONDITIONS: tuple[Condition, ...] = tuple(
    (label, arm) for label in (FB0, FB1) for arm in ("A0", "A1", "A4")
)
# The fixed sequence of 20_preregistration 2: (name, treatment, control).
HYPOTHESES: tuple[tuple[str, Condition, Condition], ...] = (
    ("H1", (FB1, "A4"), (FB0, "A4")),
    ("H2", (FB1, "A4"), (FB1, "A0")),
    ("H3", (FB1, "A4"), (FB1, "A1")),
)
# Exploratory differences between arms (20_preregistration 3).
ARM_DIFFERENCES: tuple[tuple[Condition, Condition], ...] = (
    ((FB0, "A4"), (FB0, "A1")),
    ((FB0, "A4"), (FB0, "A0")),
    ((FB0, "A1"), (FB0, "A0")),
    ((FB1, "A1"), (FB0, "A1")),
    ((FB0, "A2"), (FB0, "A0")),
    ((FB1, "A2"), (FB1, "A0")),
    ((FB0, "A5"), (FB0, "A0")),
)
# Each family of comparisons with the condition groups its metrics pair; a group is compared
# over the streams every one of its conditions completed.
COMPARISON_FAMILIES: dict[str, tuple[tuple[Condition, ...], ...]] = {
    "primary": (PRIMARY_CONDITIONS,),
    "a2": (((FB0, "A2"), (FB0, "A0")), ((FB1, "A2"), (FB1, "A0"))),
    "a5": (((FB0, "A5"), (FB0, "A0")),),
    "verify": tuple(
        (("verify", arm), (FB0, arm), ("verify", "A0"), (FB0, "A0")) for arm in ("A1", "A4")
    ),
    "probe": ((("probe", "A4"), (FB0, "A4")),),
    "null": tuple(((label, "A1"), (label, "A4")) for label in ("null", FB0)),
    "sens": tuple(
        (("sens", "A4"), ("sens", control), (FB0, "A4"), (FB0, control)) for control in ("A1", "A0")
    ),
}
SESSION_BANDS: dict[str, range] = {"s1-4": range(1, 5), "s5-8": range(5, 9), "s9-12": range(9, 13)}
CLBENCH_ARMS = ("A0", "A1", "A4")
# Per-question exploration indicators (20_preregistration 4).
BEHAVIOUR_METRICS = ("queries_per_question", "unfiltered_ref_query_rate", "schema_inspection_rate")

# Per-question features by (run label, stream, arm, qid).
FeatureKey = tuple[str, str, str, str]
PerStream = dict[str, float]


def condition_label(cond: Condition) -> str:
    return f"{cond[0]}/{cond[1]}"


def _data(runs: Mapping[str, RunData], cond: Condition) -> RunData:
    if cond[0] not in runs:
        raise ValueError(f"no run labelled {cond[0]!r} for condition {condition_label(cond)}")
    return runs[cond[0]]


def _has_condition(runs: Mapping[str, RunData], cond: Condition) -> bool:
    return cond[0] in runs and cond[1] in _required_arms(runs[cond[0]])


def _required_arms(data: RunData) -> set[str]:
    return {split_job_key(key)[1] for key in data.required_jobs}


def _required_streams(data: RunData, arm: str) -> set[str]:
    return {stream for stream, a in map(split_job_key, data.required_jobs) if a == arm}


def _universe(runs: Mapping[str, RunData], conds: Sequence[Condition]) -> list[str]:
    """Streams that every condition's run asks for."""
    if not conds:
        raise ValueError("at least one condition is needed")
    sets = [_required_streams(_data(runs, cond), cond[1]) for cond in conds]
    return sorted(set.intersection(*sets))


def _completed(runs: Mapping[str, RunData], cond: Condition, stream_id: str) -> bool:
    return _data(runs, cond).status.get(job_key(stream_id, cond[1])) == "completed"


def common_streams(runs: Mapping[str, RunData], conds: Sequence[Condition]) -> list[str]:
    """Streams that every condition's run asks for and in which every condition completed.

    This is the exclusion rule of 20_preregistration 5: a stream where one condition did not
    finish is dropped from every condition of the comparison.
    """
    return [
        stream_id
        for stream_id in _universe(runs, conds)
        if all(_completed(runs, cond, stream_id) for cond in conds)
    ]


# An unfinished job: (reason, condition, stream).
_Unfinished = tuple[str, Condition, str]


def _unfinished(runs: Mapping[str, RunData], conds: Sequence[Condition]) -> set[_Unfinished]:
    return {
        (state, cond, stream_id)
        for stream_id in _universe(runs, conds)
        for cond in conds
        if (state := _data(runs, cond).status.get(job_key(stream_id, cond[1]), NOT_STARTED))
        != "completed"
    }


def _count_unfinished(jobs: set[_Unfinished]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for reason, cond, _ in jobs:
        counts[reason][condition_label(cond)] += 1
    return {reason: dict(sorted(by.items())) for reason, by in sorted(counts.items())}


def exclusions(
    runs: Mapping[str, RunData], conds: Sequence[Condition]
) -> dict[str, dict[str, int]]:
    """Unfinished jobs among the asked-for streams, by reason (job status) and condition.

    A job that never started is counted under `not_started`. Zero counts are left out.
    """
    return _count_unfinished(_unfinished(runs, conds))


def family_exclusions(
    runs: Mapping[str, RunData], groups: Sequence[Sequence[Condition]]
) -> dict[str, dict[str, int]] | None:
    """`exclusions` over every group of a comparison family, each unfinished job counted once.

    Groups with a condition that no run holds are skipped; None when every group is.
    """
    present = [g for g in groups if all(_has_condition(runs, cond) for cond in g)]
    if not present:
        return None
    return _count_unfinished(set().union(*(_unfinished(runs, g) for g in present)))


def condition_scores(
    runs: Mapping[str, RunData],
    cond: Condition,
    streams: Sequence[str],
    *,
    where: ResultFilter | None = None,
) -> dict[str, float]:
    """Mean score of `cond` in each stream; streams without matching rows are left out."""
    return per_stream_score(_data(runs, cond), cond[1], streams, where=where)


def _bootstrap(diffs: Sequence[float]) -> CI:
    return paired_bootstrap(diffs, n_resamples=BOOTSTRAP_RESAMPLES, seed=FOLLOWUP_BOOTSTRAP_SEED)


def _paired(maps: Sequence[Mapping[str, float]], streams: Sequence[str]) -> list[str]:
    return [s for s in streams if all(s in values for values in maps)]


def paired_ci(maps: Sequence[Mapping[str, float]], streams: Sequence[str], *, what: str) -> CI:
    """Paired bootstrap over the streams of `streams` present in every map.

    Two maps give `treat - ctrl` per stream; four maps give `(a1 - a0) - (b1 - b0)`.

    Raises
    ------
    ValueError
        If there are not two or four maps, or no stream has a value in every map; `what` names
        the comparison in the message.
    """
    if len(maps) not in (2, 4):
        raise ValueError(f"{what}: expected 2 or 4 per-stream maps, got {len(maps)}")
    paired = _paired(maps, streams)
    if not paired:
        raise ValueError(f"no stream has values for every condition of {what}")
    if len(maps) == 2:
        treat, ctrl = maps
        return _bootstrap([treat[s] - ctrl[s] for s in paired])
    a1, a0, b1, b0 = maps
    return _bootstrap([(a1[s] - a0[s]) - (b1[s] - b0[s]) for s in paired])


def contrast(
    runs: Mapping[str, RunData],
    treat: Condition,
    ctrl: Condition,
    streams: Sequence[str],
    *,
    where: ResultFilter | None = None,
) -> CI:
    """Paired bootstrap of the per-stream score difference `treat - ctrl` (seed 20261111).

    Raises
    ------
    ValueError
        If no stream in `streams` has rows for both conditions.
    """
    return paired_ci(
        [condition_scores(runs, cond, streams, where=where) for cond in (treat, ctrl)],
        streams,
        what=f"{condition_label(treat)} - {condition_label(ctrl)}",
    )


def interaction(
    runs: Mapping[str, RunData],
    a1: Condition,
    a0: Condition,
    b1: Condition,
    b0: Condition,
    streams: Sequence[str],
    *,
    where: ResultFilter | None = None,
) -> CI:
    """Paired bootstrap of the per-stream `(a1 - a0) - (b1 - b0)` score difference.

    Raises
    ------
    ValueError
        If no stream in `streams` has rows for all four conditions.
    """
    scores = [condition_scores(runs, cond, streams, where=where) for cond in (a1, a0, b1, b0)]
    names = [condition_label(cond) for cond in (a1, a0, b1, b0)]
    return paired_ci(scores, streams, what=f"({names[0]} - {names[1]}) - ({names[2]} - {names[3]})")


def verdict(ci: CI) -> Verdict:
    """Supported when the 95% interval lies above 0, reversed when below, else not shown."""
    if ci.low > 0:
        return "supported"
    if ci.high < 0:
        return "reversed"
    return "not_shown"


def fixed_sequence(
    results: Sequence[tuple[str, CI]], *, n_streams: int, required_streams: int = REQUIRED_STREAMS
) -> list[dict[str, Any]]:
    """Verdicts of the hypotheses in their fixed order.

    A hypothesis is judged confirmatorily only when every earlier one was supported; with fewer
    than `required_streams` analyzed streams none is (20_preregistration 2).
    """
    open_gate = n_streams >= required_streams
    judged: list[dict[str, Any]] = []
    for name, ci in results:
        found = verdict(ci)
        judged.append(
            {
                "hypothesis": name,
                "verdict": found,
                "confirmatory": open_gate,
                "practical_target_met": ci.mean >= PRACTICAL_TARGET,
                **ci.model_dump(),
            }
        )
        open_gate = open_gate and found == "supported"
    return judged


def partial_sessions(
    runs: Mapping[str, RunData], conds: Sequence[Condition]
) -> dict[str, list[int]]:
    """Sessions with results in every condition, for each excluded stream that has any.

    Results are written only when a session finishes, so these are the sessions every condition
    completed in a stream that the exclusion rule dropped (20_preregistration 5).
    """
    analyzed = set(common_streams(runs, conds))
    partial: dict[str, list[int]] = {}
    for stream_id in _universe(runs, conds):
        if stream_id in analyzed:
            continue
        per_condition = [
            {
                r.session
                for r in _data(runs, cond).results
                if (r.stream_id, r.arm) == (stream_id, cond[1])
            }
            for cond in conds
        ]
        shared = set.intersection(*per_condition)
        if shared:
            partial[stream_id] = sorted(shared)
    return partial


@dataclass(frozen=True)
class _KeepSessions:
    """Result filter: every row of a complete stream, and the listed sessions of a partial one."""

    complete: frozenset[str]
    partial: Mapping[str, Sequence[int]]

    def __call__(self, row: QuestionResult) -> bool:
        return row.stream_id in self.complete or row.session in self.partial.get(row.stream_id, ())


def merge_runs(runs: Sequence[RunData]) -> RunData:
    """One `RunData` holding several runs over disjoint streams (CL-Bench repetitions).

    Raises
    ------
    ValueError
        If two runs hold the same job.
    """
    status: dict[str, Any] = {}
    for data in runs:
        overlap = sorted(set(status) & set(data.status))
        if overlap:
            raise ValueError(f"runs to merge share jobs: {overlap}")
        status |= data.status
    return RunData(
        required_jobs=[key for data in runs for key in data.required_jobs],
        results=[row for data in runs for row in data.results],
        probes=[probe for data in runs for probe in data.probes],
        consolidations=[record for data in runs for record in data.consolidations],
        status=status,
        costs=[row for data in runs for row in data.costs],
    )


def clbench_summary(data: RunData) -> dict[str, Any]:
    """A4 - A1 and A4 - A0 on the official score and the accuracy, over the completed streams."""
    streams = analyzable_streams(data, CLBENCH_ARMS)
    summary: dict[str, Any] = {"n_streams": len(streams), "streams": streams}
    if not streams:
        return summary
    score = {arm: per_stream_score(data, arm, streams) for arm in CLBENCH_ARMS}
    accuracy = {arm: per_stream_accuracy(data, arm, streams) for arm in CLBENCH_ARMS}
    summary["by_arm"] = {
        arm: {"score": fmean(score[arm].values()), "accuracy": fmean(accuracy[arm].values())}
        for arm in CLBENCH_ARMS
    }
    for control in ("A1", "A0"):
        name = f"A4-{control}"
        summary[name] = {
            "score": paired_ci([score["A4"], score[control]], streams, what=name).model_dump(),
            "accuracy": paired_ci(
                [accuracy["A4"], accuracy[control]], streams, what=name
            ).model_dump(),
        }
    return summary


# --- reading streams, transcripts and memory snapshots ----------------------------------------


@dataclass(frozen=True)
class _Inputs:
    """Everything `summarize_followup` reads from disk besides the result rows."""

    run_dirs: Mapping[str, Path]
    runs: Mapping[str, RunData]
    streams: Mapping[str, Stream]
    features: Mapping[FeatureKey, QuestionFeatures]


def _load_streams(run_dirs: Mapping[str, Path], streams_root: Path) -> dict[str, Stream]:
    stream_ids = sorted(
        {s for run_dir in run_dirs.values() for s in load_manifest(run_dir).config.stream_ids}
    )
    return {stream_id: load_stream(streams_root / stream_id) for stream_id in stream_ids}


def _read_features(
    label: str, run_dir: Path, data: RunData, streams: Mapping[str, Stream]
) -> dict[FeatureKey, QuestionFeatures]:
    """Features of every question with a result row, read from its session's transcript."""
    layout = RunLayout(run_dir)
    sessions = sorted({(r.stream_id, r.arm, r.session) for r in data.results})
    ref_tables = {s: reference_table(streams[s].root) for s in {key[0] for key in sessions}}
    features: dict[FeatureKey, QuestionFeatures] = {}
    for stream_id, arm, session in sessions:
        transcript = layout.transcript_file(stream_id, arm, session).read_text(encoding="utf-8")
        qids = [q.qid for q in streams[stream_id].questions_for(session)]
        for found in question_features(transcript, ref_table=ref_tables[stream_id], qids=qids):
            features[(label, stream_id, arm, found.qid)] = found
    return features


def _injected_text(run_dir: Path, stream_id: str, arm: str, session: int) -> str:
    """`injected.md` after the consolidation of `session`; empty when the arm never wrote one.

    Raises
    ------
    FileNotFoundError
        If the session left no snapshot.
    """
    snapshot = RunLayout(run_dir).snapshot_dir(stream_id, arm, session)
    if not snapshot.is_dir():
        raise FileNotFoundError(f"memory snapshot not found: {snapshot}")
    path = snapshot / "memory" / "injected.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _snapshot_mean(
    inputs: _Inputs,
    cond: Condition,
    streams: Sequence[str],
    count: Callable[[str, Stream], int],
    first_session: Callable[[Stream], int | None],
) -> PerStream:
    """Per stream, the mean of `count` over the snapshots from `first_session` to the 11th.

    Snapshot k is the memory after session k's consolidation; the last session's snapshot never
    reaches an agent and is left out. Streams where `first_session` is None are left out.
    """
    label, arm = cond
    values: PerStream = {}
    for stream_id in streams:
        stream = inputs.streams[stream_id]
        first = first_session(stream)
        if first is None or first >= stream.sessions:
            continue
        counts = [
            count(_injected_text(inputs.run_dirs[label], stream_id, arm, k), stream)
            for k in range(first, stream.sessions)
        ]
        values[stream_id] = fmean(counts)
    return values


def _first_session(_: Stream) -> int:
    return 1


def _soft_delete_start(stream: Stream) -> int | None:
    starts = [f.valid_from_session for f in stream.facts if f.kind == "soft_delete"]
    return min(starts) if starts else None


def _change_count(text: str, _: Stream) -> int:
    return count_change_records(text)


def _void_count(text: str, stream: Stream) -> int:
    return count_void_policy_lines(text, void_columns=void_columns(stream))


# --- per-condition values -------------------------------------------------------------------


def _rows(inputs: _Inputs, cond: Condition, streams: Sequence[str]) -> list[QuestionResult]:
    wanted = set(streams)
    return [
        r for r in _data(inputs.runs, cond).results if r.arm == cond[1] and r.stream_id in wanted
    ]


def _behaviour(inputs: _Inputs, cond: Condition, streams: Sequence[str]) -> dict[str, PerStream]:
    """Per-stream means of the exploration indicators of `cond`."""
    label, arm = cond
    by_stream: dict[str, list[QuestionResult]] = defaultdict(list)
    for row in _rows(inputs, cond, streams):
        by_stream[row.stream_id].append(row)
    metrics: dict[str, PerStream] = {name: {} for name in BEHAVIOUR_METRICS}
    for stream_id, rows in by_stream.items():
        found = [inputs.features[(label, stream_id, arm, r.qid)] for r in rows]
        # Successful queries only (20_preregistration 4); attempts include errors and refusals.
        metrics["queries_per_question"][stream_id] = fmean(r.ok_queries for r in rows)
        metrics["unfiltered_ref_query_rate"][stream_id] = fmean(
            float(f.unfiltered_ref_query) for f in found
        )
        metrics["schema_inspection_rate"][stream_id] = fmean(
            float(f.schema_inspection) for f in found
        )
    return metrics


def _condition_summary(inputs: _Inputs, cond: Condition) -> dict[str, Any]:
    data = _data(inputs.runs, cond)
    arm = cond[1]
    streams = common_streams(inputs.runs, [cond])
    rows = _rows(inputs, cond, streams)
    consolidations = [c for c in data.consolidations if c.arm == arm and c.stream_id in streams]
    behaviour = _behaviour(inputs, cond, streams)
    return {
        "n_streams": len(streams),
        "accuracy": mean_or_none(per_stream_accuracy(data, arm, streams).values()),
        "score": mean_or_none(per_stream_score(data, arm, streams).values()),
        "error_breakdown": error_breakdown(rows),
        "no_submission_rate": no_submission_rate(rows),
        "note_written_rate": mean_or_none(float(r.note_written) for r in rows),
        "retrieved_tokens_mean": mean_or_none(float(r.retrieved_tokens) for r in rows),
        "consolidation_sql_queries_mean": mean_or_none(
            float(c.sql_queries) for c in consolidations
        ),
        "consolidation_notes_files_read_mean": mean_or_none(
            float(c.notes_files_read) for c in consolidations
        ),
        **{name: mean_or_none(behaviour[name].values()) for name in BEHAVIOUR_METRICS},
        **arm_cost(data, arm, streams, correct_answers=sum(r.correct for r in rows)),
    }


def _ci_or_none(
    maps: Sequence[Mapping[str, float]], streams: Sequence[str]
) -> dict[str, Any] | None:
    """The paired difference (two maps) or interaction (four maps), or None without pairs."""
    if not _paired(maps, streams):
        return None
    return paired_ci(maps, streams, what="comparison").model_dump()


def _scores(
    inputs: _Inputs, conds: Sequence[Condition], streams: Sequence[str], where: ResultFilter | None
) -> list[PerStream]:
    return [condition_scores(inputs.runs, cond, streams, where=where) for cond in conds]


# --- sections of followup-metrics.json --------------------------------------------------------


def _primary(inputs: _Inputs) -> dict[str, Any]:
    analyzed = common_streams(inputs.runs, PRIMARY_CONDITIONS)
    hypotheses = None
    if analyzed:
        results = [
            (name, contrast(inputs.runs, treat, ctrl, analyzed)) for name, treat, ctrl in HYPOTHESES
        ]
        hypotheses = fixed_sequence(results, n_streams=len(analyzed))
        for judged, (_, treat, ctrl) in zip(hypotheses, HYPOTHESES, strict=True):
            judged |= {"treatment": condition_label(treat), "control": condition_label(ctrl)}
    else:
        logger.warning("No stream completed all six primary conditions; no hypothesis is judged")
    return {
        "conditions": [condition_label(cond) for cond in PRIMARY_CONDITIONS],
        "streams": {
            "analyzed": analyzed,
            "n": len(analyzed),
            "required": REQUIRED_STREAMS,
            "excluded": exclusions(inputs.runs, PRIMARY_CONDITIONS),
        },
        "hypotheses": hypotheses,
        "missing_data_sensitivity": _missing_data_sensitivity(inputs, analyzed),
    }


def _missing_data_sensitivity(inputs: _Inputs, analyzed: Sequence[str]) -> dict[str, Any]:
    """The hypotheses again, adding the shared completed sessions of the excluded streams."""
    partial = partial_sessions(inputs.runs, PRIMARY_CONDITIONS)
    streams = sorted({*analyzed, *partial})
    keep = _KeepSessions(complete=frozenset(analyzed), partial=partial)
    hypotheses = {
        name: _ci_or_none(_scores(inputs, (treat, ctrl), streams, keep), streams)
        for name, treat, ctrl in HYPOTHESES
    }
    return {"streams_added": partial, "hypotheses": hypotheses if partial else None}


def _secondary(inputs: _Inputs) -> dict[str, Any]:
    analyzed = common_streams(inputs.runs, PRIMARY_CONDITIONS)
    conds = ((FB1, "A4"), (FB0, "A4"), (FB1, "A1"), (FB0, "A1"))
    return {
        "interaction_feedback_a4_vs_a1": _ci_or_none(
            _scores(inputs, conds, analyzed, None), analyzed
        ),
        "aa_check_a0": _ci_or_none(
            _scores(inputs, ((FB1, "A0"), (FB0, "A0")), analyzed, None), analyzed
        ),
    }


def _comparison(inputs: _Inputs, treat: Condition, ctrl: Condition) -> dict[str, Any] | None:
    if not (_has_condition(inputs.runs, treat) and _has_condition(inputs.runs, ctrl)):
        return None
    streams = common_streams(inputs.runs, [treat, ctrl])
    return {
        "treatment": condition_label(treat),
        "control": condition_label(ctrl),
        "n_streams": len(streams),
        "ci": _ci_or_none(_scores(inputs, (treat, ctrl), streams, None), streams),
    }


def _strata(inputs: _Inputs) -> dict[str, Any]:
    """Accuracy and the hypotheses' contrasts within each stratum, on the primary streams."""
    analyzed = common_streams(inputs.runs, PRIMARY_CONDITIONS)
    kinds = {
        (stream_id, qid): found
        for stream_id in analyzed
        for qid, found in question_drift_kinds(inputs.streams[stream_id]).items()
    }
    strata: dict[str, ResultFilter] = {
        "drift_dependent": lambda r: r.drift_dependent,
        "drift_independent": lambda r: not r.drift_dependent,
    }
    for band, sessions in SESSION_BANDS.items():
        strata[f"sessions_{band}"] = _InSessions(sessions)
    for kind in sorted({k for found in kinds.values() for k in found}):
        strata[f"drift_kind_{kind}"] = _HasDriftKind(kind, kinds)
    report: dict[str, Any] = {}
    for name, where in strata.items():
        accuracy = {
            condition_label(cond): mean_or_none(
                per_stream_accuracy(
                    _data(inputs.runs, cond), cond[1], analyzed, where=where
                ).values()
            )
            for cond in PRIMARY_CONDITIONS
        }
        contrasts = {
            hypothesis: _ci_or_none(_scores(inputs, (treat, ctrl), analyzed, where), analyzed)
            for hypothesis, treat, ctrl in HYPOTHESES
        }
        report[name] = {"accuracy": accuracy, "contrasts": contrasts}
    return report


@dataclass(frozen=True)
class _InSessions:
    sessions: range

    def __call__(self, row: QuestionResult) -> bool:
        return row.session in self.sessions


@dataclass(frozen=True)
class _HasDriftKind:
    kind: str
    kinds: Mapping[tuple[str, str], Sequence[str]]

    def __call__(self, row: QuestionResult) -> bool:
        return self.kind in self.kinds.get((row.stream_id, row.qid), ())


def _mechanism(inputs: _Inputs) -> dict[str, Any] | None:
    """Verification instruction x memory (streams fu-01..20 that ran `verify`)."""
    if "verify" not in inputs.runs:
        return None
    report: dict[str, Any] = {}
    for arm in ("A1", "A4"):
        conds = (("verify", arm), (FB0, arm), ("verify", "A0"), (FB0, "A0"))
        streams = common_streams(inputs.runs, conds)
        behaviour = [_behaviour(inputs, cond, streams) for cond in conds]
        report[f"interaction_{arm}_vs_A0"] = {
            "n_streams": len(streams),
            "score": _ci_or_none(_scores(inputs, conds, streams, None), streams),
            **{
                name: _ci_or_none([b[name] for b in behaviour], streams)
                for name in BEHAVIOUR_METRICS
            },
        }
    for arm in ("A0", "A1", "A4"):
        report[f"verify-fb0_{arm}"] = _comparison(inputs, ("verify", arm), (FB0, arm))
    return report


def _probe(inputs: _Inputs) -> dict[str, Any] | None:
    """A4 with consolidator queries against A4 of fb0 on the same streams."""
    if "probe" not in inputs.runs:
        return None
    conds = (("probe", "A4"), (FB0, "A4"))
    streams = common_streams(inputs.runs, conds)
    behaviour = [_behaviour(inputs, cond, streams) for cond in conds]
    void_lines = [
        _snapshot_mean(inputs, cond, streams, _void_count, _soft_delete_start) for cond in conds
    ]
    return {
        "n_streams": len(streams),
        "score": _ci_or_none(_scores(inputs, conds, streams, None), streams),
        **{name: _ci_or_none([b[name] for b in behaviour], streams) for name in BEHAVIOUR_METRICS},
        "void_policy_lines": {
            **{
                condition_label(cond): mean_or_none(values.values())
                for cond, values in zip(conds, void_lines, strict=True)
            },
            "difference": _ci_or_none(void_lines, streams),
        },
    }


def _probe_accuracy(data: RunData, arm: str, streams: Sequence[str]) -> float | None:
    by_stream: dict[str, list[float]] = defaultdict(list)
    for probe in data.probes:
        if probe.arm == arm and probe.stream_id in streams:
            by_stream[probe.stream_id].append(float(probe.correct))
    return mean_or_none(fmean(values) for values in by_stream.values())


def _null(inputs: _Inputs) -> dict[str, Any] | None:
    """Memory-probe accuracy and change records without drift, next to the same arms of fb0."""
    if "null" not in inputs.runs:
        return None
    report: dict[str, Any] = {}
    for label in ("null", FB0):
        conds = [(label, "A1"), (label, "A4")]
        streams = common_streams(inputs.runs, conds)
        report[label] = {
            "n_streams": len(streams),
            **{
                condition_label(cond): {
                    "probe_accuracy": _probe_accuracy(_data(inputs.runs, cond), cond[1], streams),
                    "change_records_per_snapshot": mean_or_none(
                        _snapshot_mean(
                            inputs, cond, streams, _change_count, _first_session
                        ).values()
                    ),
                }
                for cond in conds
            },
        }
    return report


def _sensitivity(inputs: _Inputs) -> dict[str, Any] | None:
    """`fu_sens` (agent effort medium) next to fb0 on the same streams."""
    if "sens" not in inputs.runs:
        return None
    report: dict[str, Any] = {}
    for control in ("A1", "A0"):
        conds = [("sens", "A4"), ("sens", control), (FB0, "A4"), (FB0, control)]
        streams = common_streams(inputs.runs, conds)
        scores = _scores(inputs, conds, streams, None)
        report[f"A4-{control}"] = {
            "n_streams": len(streams),
            "sens": _ci_or_none(scores[:2], streams),
            "fb0": _ci_or_none(scores[2:], streams),
        }
    return report


def _conditions(runs: Mapping[str, RunData]) -> list[Condition]:
    return [(label, arm) for label, data in runs.items() for arm in sorted(_required_arms(data))]


def summarize_followup(
    run_dirs: Mapping[str, Path],
    *,
    ledger: Path,
    streams_root: Path,
    clbench_dirs: Sequence[Path],
) -> dict[str, Any]:
    """Every metric of 20_preregistration 2-5 as a JSON-serialisable dict.

    Parameters
    ----------
    run_dirs
        SchemaDrift run directories by label (`fb0`, `fb1`, and optionally `verify`, `probe`,
        `null`, `sens`). `fb0` and `fb1` are required.
    ledger
        The shared cost ledger.
    streams_root
        Directory holding each stream's directory (`stream.json` and epoch databases).
    clbench_dirs
        CL-Bench run directories of the first study and of the follow-up, pooled.

    Raises
    ------
    ValueError
        If `fb0` or `fb1` is missing.
    """
    missing = [label for label in (FB0, FB1) if label not in run_dirs]
    if missing:
        raise ValueError(f"the follow-up analysis needs the runs {missing}")
    runs = {label: load_run(run_dir, ledger) for label, run_dir in run_dirs.items()}
    streams = _load_streams(run_dirs, streams_root)
    features: dict[FeatureKey, QuestionFeatures] = {}
    for label, run_dir in run_dirs.items():
        features |= _read_features(label, run_dir, runs[label], streams)
    inputs = _Inputs(run_dirs=run_dirs, runs=runs, streams=streams, features=features)
    clbench = (
        clbench_summary(merge_runs([load_run(d, ledger) for d in clbench_dirs]))
        if clbench_dirs
        else None
    )
    metrics: dict[str, Any] = {
        "bootstrap": {"seed": FOLLOWUP_BOOTSTRAP_SEED, "resamples": BOOTSTRAP_RESAMPLES},
        "runs": {label: run_dir.name for label, run_dir in run_dirs.items()},
        "primary": _primary(inputs),
        "excluded": {
            family: family_exclusions(runs, groups)
            for family, groups in COMPARISON_FAMILIES.items()
        },
        "secondary": _secondary(inputs),
        "exploratory": {
            "arm_differences": {
                f"{condition_label(t)}-{condition_label(c)}": _comparison(inputs, t, c)
                for t, c in ARM_DIFFERENCES
            },
            "strata": _strata(inputs),
            "mechanism": _mechanism(inputs),
            "probe": _probe(inputs),
            "null": _null(inputs),
            "sensitivity": _sensitivity(inputs),
            "clbench": clbench,
        },
        "by_condition": {
            condition_label(cond): _condition_summary(inputs, cond) for cond in _conditions(runs)
        },
    }
    logger.info(
        "Follow-up metrics: %d runs, %d primary streams",
        len(runs),
        metrics["primary"]["streams"]["n"],
    )
    return metrics
