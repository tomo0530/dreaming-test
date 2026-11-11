"""Analyze the follow-up study: followup-metrics.json, or the pilot gate with --gate.

Usage: uv run python scripts/analyze_followup.py --run fb0=<run_dir> --run fb1=<run_dir> ...
       [--clbench <run_dir>]... --out <dir>
       [--streams data/streams] [--ledger outputs/cost_ledger.sqlite]
       uv run python scripts/analyze_followup.py --gate --run fb0=<pilot_dir> ... (5 pilot runs)
       --main-config fb0=configs/experiments/followup/fu_fb0.yaml ... --out <dir>

Without --gate the runs are the main runs labelled fb0, fb1, verify, probe, null and sens, and
--clbench names every CL-Bench run to pool (the first study's and the follow-up's). With --gate
the runs are the pilot runs labelled fb0, fb1, verify, probe and null, and each --main-config
gives the main configuration with the same label (fu_main and fu_aux; not fu_sens) whose jobs
the pilot costs are projected onto. The gate writes followup-gate.json and exits with status 1
when it does not pass.
"""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from project_module.config import load_experiment
from project_module.evaluation.followup import summarize_followup
from project_module.evaluation.followup_gate import (
    count_main_jobs,
    evaluate_followup_gate,
    stream_validation_problems,
)
from project_module.layout import ledger_path
from project_module.manifest import load_manifest
from project_module.runner import configure_logging

METRICS_FILE = "followup-metrics.json"
GATE_FILE = "followup-gate.json"
_LABEL = re.compile(r"^[a-z0-9_]+$")


def _labelled(
    parser: argparse.ArgumentParser, values: Sequence[str], option: str
) -> dict[str, Path]:
    """Parse repeated `label=path` values; labels are lower-case words and must be unique."""
    parsed: dict[str, Path] = {}
    for value in values:
        label, sep, path = value.partition("=")
        if not sep or not path:
            parser.error(f"{option} expects label=path, got {value!r}")
        if not _LABEL.match(label):
            parser.error(f"{option} label must match {_LABEL.pattern}, got {label!r}")
        if label in parsed:
            parser.error(f"{option} label {label!r} is given twice")
        parsed[label] = Path(path)
    return parsed


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _gate(args: argparse.Namespace, runs: dict[str, Path], configs: dict[str, Path]) -> int:
    main_configs = {label: load_experiment(path) for label, path in configs.items()}
    main_jobs, phase_caps = count_main_jobs(main_configs)
    stream_ids = {s for run_dir in runs.values() for s in load_manifest(run_dir).config.stream_ids}
    stream_ids |= {s for config in main_configs.values() for s in config.stream_ids}
    result = evaluate_followup_gate(
        runs,
        ledger=args.ledger,
        validation_problems=stream_validation_problems(sorted(stream_ids), args.streams),
        main_jobs=main_jobs,
        phase_caps=phase_caps,
    )
    _write_json(args.out / GATE_FILE, result)
    print(f"gate passed: {result['passed']}")
    for name in result["failed"]:
        print(f"failed: {name}")
    return 0 if result["passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--run", action="append", default=[], help="label=run_dir (repeatable)")
    parser.add_argument("--clbench", type=Path, action="append", default=[], help="CL-Bench run")
    parser.add_argument("--gate", action="store_true", help="evaluate the pilot gate G0-G7")
    parser.add_argument(
        "--main-config", action="append", default=[], help="label=main yaml (with --gate)"
    )
    parser.add_argument(
        "--streams", type=Path, default=Path("data/streams"), help="default: %(default)s"
    )
    parser.add_argument("--ledger", type=Path, default=ledger_path(), help="default: %(default)s")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    args = parser.parse_args(argv)
    runs = _labelled(parser, args.run, "--run")
    configs = _labelled(parser, args.main_config, "--main-config")
    if not runs:
        parser.error("give at least one --run")
    if args.gate and not configs:
        parser.error("--gate needs --main-config")
    if not args.gate and configs:
        parser.error("--main-config is only used with --gate")
    if args.gate and args.clbench:
        parser.error("--clbench is not used with --gate")

    configure_logging()
    if args.gate:
        return _gate(args, runs, configs)
    metrics = summarize_followup(
        runs, ledger=args.ledger, streams_root=args.streams, clbench_dirs=args.clbench
    )
    _write_json(args.out / METRICS_FILE, metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
