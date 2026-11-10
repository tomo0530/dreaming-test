"""Read the artifacts of one run (result files and the cost ledger) into `RunData`."""

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel, ValidationError

from project_module.config import ArmName
from project_module.contracts import (
    ConsolidationRecord,
    ProbeResult,
    QuestionResult,
    RunStatus,
)
from project_module.layout import RunLayout
from project_module.llm.ledger import COMMITTED_USD_SQL
from project_module.manifest import load_manifest

logger = logging.getLogger(__name__)

# Rows are priced exactly as the ledger counts them against its caps.
_COST_QUERY = f"""
SELECT phase, stream_id, arm, session, component, COUNT(*) AS calls,
       SUM({COMMITTED_USD_SQL}) AS cost_usd
FROM reservations
WHERE run_id = ?
GROUP BY phase, stream_id, arm, session, component
ORDER BY stream_id, arm, session, component
"""


class RunData(BaseModel):
    """Everything one run produced, flattened across jobs.

    `status` is keyed by `job_key(stream_id, arm)`; `required_jobs` lists the keys of every
    job the run's manifest asks for, started or not. Each entry of `costs` is one ledger
    aggregate with the keys phase, stream_id, arm, session, component, calls and cost_usd.
    """

    required_jobs: list[str]
    results: list[QuestionResult]
    probes: list[ProbeResult]
    consolidations: list[ConsolidationRecord]
    status: dict[str, RunStatus]
    costs: list[dict[str, Any]]


class JobStatus(BaseModel):
    """Content of a job's `status.json`, written by the runner."""

    status: RunStatus
    completed_sessions: int


def job_key(stream_id: str, arm: str) -> str:
    return f"{stream_id}/{arm}"


def split_job_key(key: str) -> tuple[str, str]:
    stream_id, _, arm = key.rpartition("/")
    return stream_id, arm


def analyzable_streams(data: RunData, arms: Sequence[str]) -> list[str]:
    """Streams in which every arm in `arms` has a completed job.

    This is the preregistered exclusion rule: a stream where any arm did not finish is dropped
    for all arms, never for one arm only.
    """
    stream_ids = {split_job_key(key)[0] for key in data.status}
    return sorted(
        stream_id
        for stream_id in stream_ids
        if all(data.status.get(job_key(stream_id, arm)) == "completed" for arm in arms)
    )


def load_run(run_dir: Path, ledger: Path) -> RunData:
    """Load a run directory and the costs the ledger recorded for it.

    The ledger's `run_id` is the run directory's name. Rows are de-duplicated on
    (stream, arm, session, question or fact) with the last occurrence winning. A job directory
    without a `status.json` has not finished a session yet and is reported as `running`. The
    required jobs come from the configuration in `manifest.json`.

    Raises
    ------
    FileNotFoundError
        If the run directory, its manifest or the ledger does not exist.
    """
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run directory not found: {run_dir}")
    if not ledger.is_file():
        raise FileNotFoundError(f"cost ledger not found: {ledger}")
    config = load_manifest(run_dir).config
    required_jobs = [
        job_key(stream_id, arm)
        for stream_id in config.stream_ids
        for arm in config.arms_for(stream_id)
    ]

    layout = RunLayout(run_dir)
    results: dict[tuple[str, str, int, str], QuestionResult] = {}
    probes: dict[tuple[str, str, int, str], ProbeResult] = {}
    consolidations: dict[tuple[str, str, int], ConsolidationRecord] = {}
    status: dict[str, RunStatus] = {}
    for stream_id, arm in _discover_jobs(layout):
        status[job_key(stream_id, arm)] = _read_status(layout.status_file(stream_id, arm))
        for row in _read_rows(layout.results_file(stream_id, arm, 1), QuestionResult):
            results[(row.stream_id, row.arm, row.session, row.qid)] = row
        for probe in _read_rows(layout.probes_file(stream_id, arm, 1), ProbeResult):
            probes[(probe.stream_id, probe.arm, probe.session, probe.fact_id)] = probe
        for record in _read_rows(layout.consolidation_file(stream_id, arm, 1), ConsolidationRecord):
            consolidations[(record.stream_id, record.arm, record.session)] = record

    data = RunData(
        required_jobs=required_jobs,
        results=list(results.values()),
        probes=list(probes.values()),
        consolidations=list(consolidations.values()),
        status=status,
        costs=_read_costs(ledger, run_id=run_dir.name),
    )
    logger.info(
        "Loaded run %s: %d jobs, %d results, %d probes, %d consolidations, %d cost rows",
        run_dir.name,
        len(data.status),
        len(data.results),
        len(data.probes),
        len(data.consolidations),
        len(data.costs),
    )
    return data


def _discover_jobs(layout: RunLayout) -> list[tuple[str, str]]:
    arms = get_args(ArmName)
    return sorted(
        (stream_dir.name, arm)
        for stream_dir in layout.run_dir.iterdir()
        if stream_dir.is_dir()
        for arm in arms
        if layout.job_dir(stream_dir.name, arm).is_dir()
    )


def _read_status(path: Path) -> RunStatus:
    if not path.is_file():
        return "running"
    return _parse(path, JobStatus, path.read_text("utf-8")).status


def _read_rows[T: BaseModel](first_session_file: Path, model: type[T]) -> Iterator[T]:
    """Yield the rows of every `session_*.jsonl` next to the file for session 1, in name order."""
    for path in sorted(first_session_file.parent.glob("session_*.jsonl")):
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                yield _parse(path, model, line)


def _parse[T: BaseModel](path: Path, model: type[T], text: str) -> T:
    try:
        return model.model_validate_json(text)
    except ValidationError as error:
        raise ValueError(f"{path}: invalid {model.__name__}: {error}") from error


def _read_costs(ledger: Path, *, run_id: str) -> list[dict[str, Any]]:
    columns = ("phase", "stream_id", "arm", "session", "component", "calls", "cost_usd")
    with closing(sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(_COST_QUERY, (run_id,)).fetchall()
    return [dict(zip(columns, row, strict=True)) for row in rows]
