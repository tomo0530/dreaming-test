"""Post-hoc exploratory breakdowns of one finished run (not in the preregistration).

Usage: uv run python scripts/supplementary_analysis.py --run <run_dir> --out <file.json>
       [--ledger outputs/cost_ledger.sqlite]

Writes JSON with, for each stratum (session bands, drift-dependent or not), per-arm accuracy
and the paired bootstrap of the per-stream differences A4-A1, A4-A0, A1-A0 and A3-A0 (the same
`paired_bootstrap` as the primary analysis); the mean number of SQL queries per question; the
number of online tool calls per tool name read from the transcripts; and the outcome of Q_inj
split by whether a false note was detected in memory when the question started. Everything is
restricted to the streams that every arm completed, as in the primary analysis.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import fmean

from project_module.contracts import QuestionResult
from project_module.evaluation.load import RunData, analyzable_streams, load_run
from project_module.evaluation.stats import paired_bootstrap
from project_module.layout import ledger_path

ARMS = ("A0", "A1", "A2", "A3", "A4", "A5")
SESSION_BANDS = {"s1-4": range(1, 5), "s5-8": range(5, 9), "s9-12": range(9, 13)}
_TOOL_CALL = re.compile(r"^TOOL CALL (\w+):", re.MULTILINE)

Stratum = Callable[[QuestionResult], bool]


def _accuracy_by_stream(
    data: RunData, streams: Sequence[str], keep: Stratum
) -> dict[str, dict[str, float]]:
    hits: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for r in data.results:
        if r.stream_id in streams and keep(r):
            hits[r.arm][r.stream_id].append(r.correct)
    return {arm: {s: fmean(v) for s, v in by.items()} for arm, by in hits.items()}


def _stratum_report(
    data: RunData, streams: Sequence[str], keep: Stratum, pairs: Sequence[tuple[str, str]]
) -> dict[str, object]:
    acc = _accuracy_by_stream(data, streams, keep)
    diffs = {}
    for treatment, control in pairs:
        ci = paired_bootstrap([acc[treatment][s] - acc[control][s] for s in streams])
        diffs[f"{treatment}-{control}"] = ci.model_dump()
    return {"accuracy": {a: fmean(acc[a].values()) for a in ARMS if a in acc}, "diff": diffs}


def _in_sessions(sessions: range) -> Stratum:
    return lambda r: r.session in sessions


def _band(session: int) -> str:
    return next(name for name, rng in SESSION_BANDS.items() if session in rng)


def _tool_calls(run_dir: Path, streams: Sequence[str]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for transcript in run_dir.glob("*/A*/transcripts/session_*.txt"):
        arm, stream_id = transcript.parent.parent.name, transcript.parent.parent.parent.name
        if stream_id not in streams:
            continue
        counts[arm].update(_TOOL_CALL.findall(transcript.read_text(encoding="utf-8")))
    return {arm: dict(sorted(c.items())) for arm, c in sorted(counts.items())}


def supplementary(
    run_dir: Path, ledger: Path, pairs: Sequence[tuple[str, str]]
) -> dict[str, object]:
    data = load_run(run_dir, ledger)
    streams = analyzable_streams(data, ARMS)
    strata: dict[str, Stratum] = {name: _in_sessions(rng) for name, rng in SESSION_BANDS.items()}
    strata["drift_dependent"] = lambda r: r.drift_dependent
    strata["drift_independent"] = lambda r: not r.drift_dependent
    queries: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    q_inj: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    for r in data.results:
        if r.stream_id not in streams:
            continue
        queries[r.arm][_band(r.session)].append(r.queries)
        if r.injected_target:
            key = "note_detected" if r.active_markers else "no_note_detected"
            q_inj[r.arm][key][r.category] += 1
    return {
        "run_id": run_dir.name,
        "n_streams": len(streams),
        "strata": {
            name: _stratum_report(data, streams, keep, pairs) for name, keep in strata.items()
        },
        "mean_queries_per_question": {
            arm: {band: fmean(v) for band, v in by.items()} for arm, by in sorted(queries.items())
        },
        "online_tool_calls": _tool_calls(run_dir, streams),
        "q_inj_outcomes": {
            arm: {k: dict(c) for k, c in by.items()} for arm, by in sorted(q_inj.items())
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--run", type=Path, required=True, help="run directory")
    parser.add_argument("--out", type=Path, required=True, help="output JSON file")
    parser.add_argument("--ledger", type=Path, default=ledger_path(), help="default: %(default)s")
    args = parser.parse_args(argv)
    pairs = [("A4", "A1"), ("A4", "A0"), ("A1", "A0"), ("A3", "A0")]
    report = supplementary(args.run, args.ledger, pairs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
