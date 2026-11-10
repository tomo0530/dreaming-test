"""Analyze finished runs: metrics.json and figures, and optionally the pilot gate.

Usage: uv run python scripts/analyze.py --runs <run_dir>... --out <dir>
       [--gate --pilot <run_dir> --pilot-inj <run_dir>] [--mechanical-only]
       [--streams data/streams] [--ledger outputs/cost_ledger.sqlite]

`--mechanical-only` reports plumbing health without accuracy (for dev tuning). `--gate`
writes gate.json, prints whether the gate passed and the main-run scale (streams, questions
per session), and exits with status 1 when it did not pass.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from project_module.evaluation.report import analyze_runs, run_gate
from project_module.layout import ledger_path
from project_module.runner import configure_logging


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--runs", type=Path, nargs="+", default=[], help="run directories")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--gate", action="store_true", help="evaluate the pilot gate")
    parser.add_argument("--pilot", type=Path, help="pilot run directory (with --gate)")
    parser.add_argument("--pilot-inj", type=Path, help="pilot_inj run directory (with --gate)")
    parser.add_argument("--mechanical-only", action="store_true", help="no accuracy, no figures")
    parser.add_argument(
        "--streams", type=Path, default=Path("data/streams"), help="default: %(default)s"
    )
    parser.add_argument("--ledger", type=Path, default=ledger_path(), help="default: %(default)s")
    args = parser.parse_args(argv)
    if args.gate and (args.pilot is None or args.pilot_inj is None):
        parser.error("--gate needs --pilot and --pilot-inj")
    if not args.gate and (args.pilot is not None or args.pilot_inj is not None):
        parser.error("--pilot and --pilot-inj are only used with --gate")
    if args.gate and args.mechanical_only:
        parser.error("--gate reports accuracy and cannot be combined with --mechanical-only")
    if not args.runs and not args.gate:
        parser.error("give --runs, --gate, or both")

    configure_logging()
    if args.runs:
        analyze_runs(
            args.runs,
            out=args.out,
            ledger=args.ledger,
            streams_root=args.streams,
            mechanical_only=args.mechanical_only,
        )
    if not args.gate:
        return 0
    result = run_gate(
        args.pilot, args.pilot_inj, out=args.out, ledger=args.ledger, streams_root=args.streams
    )
    print(f"gate passed: {result.passed}")
    if result.scale is None:
        print("main scale: undetermined")
    else:
        streams, questions = result.scale
        print(f"main scale: N={streams} streams, q={questions} questions per session")
    for note in result.notes:
        print(f"note: {note}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
