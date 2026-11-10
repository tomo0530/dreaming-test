"""Run, or resume, an experiment against Azure Foundry with the shared cost ledger.

Usage: uv run --env-file .env python scripts/run_experiment.py
       --config configs/experiments/<x>.yaml --streams data/streams
       [--clbench data/external/clbench] [--resume <run_dir>] [--ledger outputs/cost_ledger.sqlite]

Reads AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY from the environment; neither is printed.
Prints the run directory. The exit status is 1 when any job did not complete; rerun with
`--resume <run_dir>` to continue every job that is not completed, failed ones included.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from project_module.config import load_experiment
from project_module.layout import ledger_path
from project_module.runner import build_client, configure_logging, incomplete_jobs, run_experiment


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--config", type=Path, required=True, help="experiment YAML")
    parser.add_argument(
        "--streams", type=Path, default=Path("data/streams"), help="default: %(default)s"
    )
    parser.add_argument(
        "--clbench", type=Path, default=Path("data/external/clbench"), help="default: %(default)s"
    )
    parser.add_argument("--resume", type=Path, help="run directory to continue")
    parser.add_argument("--ledger", type=Path, default=ledger_path(), help="default: %(default)s")
    parser.add_argument(
        "--models", type=Path, default=Path("configs/models.yaml"), help="default: %(default)s"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("outputs"), help="parent of new run directories"
    )
    args = parser.parse_args(argv)

    configure_logging()
    config = load_experiment(args.config)
    run_dir = run_experiment(
        config,
        out_root=args.out,
        streams_root=args.streams,
        clbench_root=args.clbench,
        client=build_client(config, ledger=args.ledger, models=args.models),
        resume=args.resume,
    )
    print(run_dir)
    incomplete = incomplete_jobs(config, run_dir)
    for job, status in sorted(incomplete.items()):
        logging.warning("job %s is %s", job, status)
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
