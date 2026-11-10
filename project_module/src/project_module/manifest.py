"""`manifest.json`: what a run was started with, so that it can be resumed and analyzed."""

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_serializer

from project_module.config import ExperimentConfig, Phase
from project_module.layout import RunLayout

logger = logging.getLogger(__name__)

_HASH_CHUNK_BYTES = 1 << 20


class RunManifest(BaseModel):
    run_id: str
    phase: Phase
    created_utc: str
    # None when the code was not run from a git checkout.
    git_sha: str | None
    git_dirty: bool | None
    config_sha256: str
    config: ExperimentConfig
    # Stream id -> SHA-256 of the file that defines the stream's questions.
    input_sha256: dict[str, str]

    @field_serializer("config")
    def _serialize_config(self, config: ExperimentConfig) -> dict[str, Any]:
        # Computed fields are derived on load and would be rejected as extra inputs.
        return config.model_dump(mode="json", exclude_computed_fields=True)


def config_sha256(config: ExperimentConfig) -> str:
    """Digest of the validated configuration, independent of YAML layout and comments."""
    return hashlib.sha256(config.model_dump_json().encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def git_state() -> tuple[str | None, bool | None]:
    """The checked-out commit and whether the work tree has uncommitted changes."""
    cwd = Path(__file__).resolve().parent
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        logger.warning("git state unavailable; the manifest records none: %s", error)
        return None, None
    return head.stdout.strip(), bool(status.stdout.strip())


def write_manifest(layout: RunLayout, manifest: RunManifest) -> None:
    path = layout.manifest_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_manifest(run_dir: Path) -> RunManifest:
    """Read `manifest.json` of the run in `run_dir`.

    Raises
    ------
    FileNotFoundError
        If `run_dir` holds no manifest.
    """
    path = RunLayout(run_dir).manifest_file()
    if not path.is_file():
        raise FileNotFoundError(f"no manifest.json in {run_dir}")
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
