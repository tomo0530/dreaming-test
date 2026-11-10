"""Generate and validate the SchemaDrift-SQL streams of an experiment configuration.

Usage: uv run python scripts/generate_streams.py --config configs/experiments/<x>.yaml
       --out data/streams

Writes one directory per stream (`stream.json` and the epoch databases). If any generated
stream fails validation, nothing is written and the exit status is 1.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from project_module.config import load_experiment
from project_module.manifest import file_sha256
from project_module.runner import InvalidStreamError, configure_logging, generate_streams
from project_module.schemadrift.generator import STREAM_FILE


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--config", type=Path, required=True, help="experiment YAML")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/streams"),
        help="directory that receives one subdirectory per stream (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    configure_logging()
    try:
        stream_dirs = generate_streams(load_experiment(args.config), args.out)
    except InvalidStreamError as error:
        for problem in error.problems:
            logging.error("invalid stream: %s", problem)
        logging.error("refusing to write streams with validation problems")
        return 1
    for stream_dir in stream_dirs:
        print(f"{stream_dir.name}\t{file_sha256(stream_dir / STREAM_FILE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
