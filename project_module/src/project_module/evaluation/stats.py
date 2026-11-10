"""Statistics over run data: bootstrap intervals, per-stream scores, costs and the scale rule."""

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from statistics import fmean

import numpy as np
from pydantic import BaseModel

from project_module.contracts import QuestionResult
from project_module.evaluation.load import RunData, job_key

BOOTSTRAP_SEED = 20261110
_CI_PERCENTILES = (2.5, 97.5)

# Main-run scale rule (00_design 8.2): a question count per session is only reduced from 6 to 5
# when 16 streams of 6 questions do not fit.
_FULL_QUESTIONS = 6
_REDUCED_QUESTIONS = 5

ResultFilter = Callable[[QuestionResult], bool]


class CI(BaseModel):
    mean: float
    low: float
    high: float
    n: int


def paired_bootstrap(
    diffs: Sequence[float], *, n_resamples: int = 10_000, seed: int = BOOTSTRAP_SEED
) -> CI:
    """Percentile bootstrap 95% interval for the mean of per-stream differences.

    The streams are the resampling unit: each resample draws `len(diffs)` differences with
    replacement. `mean` is the point estimate on the original sample.
    """
    if n_resamples < 1:
        raise ValueError(f"n_resamples must be positive, got {n_resamples}")
    values = np.asarray(diffs, dtype=np.float64)
    if values.size == 0:
        raise ValueError("paired_bootstrap needs at least one difference")
    if not np.all(np.isfinite(values)):
        raise ValueError("paired_bootstrap needs finite differences")
    n = int(values.size)
    if np.ptp(values) == 0.0:
        # Avoids float noise from averaging identical values and skips the resampling.
        point = float(values[0])
        return CI(mean=point, low=point, high=point, n=n)
    rng = np.random.default_rng(seed)
    resample_means = values[rng.integers(0, n, size=(n_resamples, n))].mean(axis=1)
    low, high = np.percentile(resample_means, _CI_PERCENTILES)
    return CI(mean=float(values.mean()), low=float(low), high=float(high), n=n)


def select_results(
    data: RunData, arm: str, streams: Sequence[str], where: ResultFilter | None = None
) -> list[QuestionResult]:
    """Result rows of `arm` in `streams`, optionally restricted by `where`."""
    wanted = set(streams)
    return [
        r
        for r in data.results
        if r.arm == arm and r.stream_id in wanted and (where is None or where(r))
    ]


def _per_stream_mean(
    rows: Sequence[QuestionResult], streams: Sequence[str], value: Callable[[QuestionResult], float]
) -> dict[str, float]:
    by_stream: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_stream[row.stream_id].append(value(row))
    return {s: fmean(by_stream[s]) for s in streams if s in by_stream}


def per_stream_score(
    data: RunData, arm: str, streams: Sequence[str], *, where: ResultFilter | None = None
) -> dict[str, float]:
    """Mean `score` of each stream (equal to the accuracy for SchemaDrift-SQL).

    Streams with no matching rows are left out.
    """
    return _per_stream_mean(select_results(data, arm, streams, where), streams, lambda r: r.score)


def per_stream_accuracy(
    data: RunData, arm: str, streams: Sequence[str], *, where: ResultFilter | None = None
) -> dict[str, float]:
    """Share of correct answers in each stream; streams with no matching rows are left out."""
    return _per_stream_mean(
        select_results(data, arm, streams, where), streams, lambda r: float(r.correct)
    )


def compare_arms(
    data: RunData,
    treatment: str,
    control: str,
    *,
    streams: Sequence[str],
    where: ResultFilter | None = None,
) -> CI:
    """Paired bootstrap of the per-stream score difference `treatment - control`.

    Only streams with rows for both arms are paired.
    """
    treated = per_stream_score(data, treatment, streams, where=where)
    controlled = per_stream_score(data, control, streams, where=where)
    paired = [s for s in streams if s in treated and s in controlled]
    if not paired:
        raise ValueError(f"no stream has rows for both {treatment} and {control}")
    return paired_bootstrap([treated[s] - controlled[s] for s in paired])


def session_accuracy(data: RunData, arm: str, streams: Sequence[str]) -> dict[int, float]:
    """Accuracy per session, averaging the streams with equal weight."""
    cells: dict[tuple[int, str], list[float]] = defaultdict(list)
    for row in select_results(data, arm, streams):
        cells[(row.session, row.stream_id)].append(float(row.correct))
    by_session: dict[int, list[float]] = defaultdict(list)
    for (session, _), values in cells.items():
        by_session[session].append(fmean(values))
    return {session: fmean(by_session[session]) for session in sorted(by_session)}


def cost_per_stream_arm(data: RunData) -> dict[str, dict[str, float]]:
    """USD per component for each `"<stream_id>/<arm>"` job."""
    jobs: dict[str, dict[str, float]] = {}
    for row in data.costs:
        components = jobs.setdefault(job_key(row["stream_id"], row["arm"]), {})
        components[row["component"]] = components.get(row["component"], 0.0) + row["cost_usd"]
    return jobs


def plan_main_scale(
    c_online: float,
    c_fixed: float,
    *,
    main_cap_usd: float = 120.0,
    stretch_cap_usd: float = 130.0,
    max_streams: int = 20,
    min_streams: int = 16,
) -> tuple[int, int] | None:
    """Choose `(streams, questions per session)` for the main run, or None if none fits.

    `c_online` is the per-stream cost of all six arms that grows with the question count;
    `c_fixed` is the part that does not (consolidation, probes, embeddings). Six questions are
    tried first, then five. If neither fits `main_cap_usd` with at least `min_streams` streams,
    `min_streams` streams of five questions are accepted when they fit `stretch_cap_usd`.
    """
    if c_online < 0 or c_fixed < 0 or c_online + c_fixed <= 0:
        raise ValueError(
            f"unit cost must be non-negative and not zero, got online={c_online} fixed={c_fixed}"
        )
    unit_cost = {
        questions: c_online * questions / _FULL_QUESTIONS + c_fixed
        for questions in (_FULL_QUESTIONS, _REDUCED_QUESTIONS)
    }
    for questions in (_FULL_QUESTIONS, _REDUCED_QUESTIONS):
        streams = min(max_streams, math.floor(main_cap_usd / unit_cost[questions]))
        if streams >= min_streams:
            return streams, questions
    if min_streams * unit_cost[_REDUCED_QUESTIONS] <= stretch_cap_usd:
        return min_streams, _REDUCED_QUESTIONS
    return None
