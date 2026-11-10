"""Fetch the pinned CL-Bench database_exploration data into data/external/clbench.

Usage: uv run --env-file .env python scripts/fetch_clbench.py [--dest DIR]

Reads HF_TOKEN from the environment (optional) and sends it to Hugging Face only. The data is
Git-ignored: the question files carry a canary string and the databases have no license notice,
so they are for internal research use only.
"""

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from project_module.clbench import fetch_clbench


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path("data/external/clbench"),
        help="destination directory (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    fetch_clbench(args.dest, hf_token=os.environ.get("HF_TOKEN") or None)
    logging.info("CL-Bench data is ready in %s", args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
