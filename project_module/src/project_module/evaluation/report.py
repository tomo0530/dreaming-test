"""File outputs of the analysis: `metrics.json`, figures and `gate.json` for finished runs."""

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from project_module.clbench import PRE_DRIFT_COUNT
from project_module.config import ExperimentConfig
from project_module.evaluation.figures import plot_pareto, plot_session_accuracy
from project_module.evaluation.gate import GateConfig, GateResult, evaluate_gate
from project_module.evaluation.load import load_run
from project_module.evaluation.summary import mechanical_summary, summarize_run
from project_module.manifest import load_manifest
from project_module.schemadrift.generator import load_stream
from project_module.schemadrift.models import SESSIONS_PER_EPOCH, Stream
from project_module.schemadrift.validate import validate_stream

logger = logging.getLogger(__name__)

METRICS_FILE = "metrics.json"
GATE_FILE = "gate.json"


def analyze_runs(
    run_dirs: Sequence[Path],
    *,
    out: Path,
    ledger: Path,
    streams_root: Path,
    mechanical_only: bool,
) -> dict[str, Any]:
    """Write `metrics.json` (keyed by run directory name) and, unless `mechanical_only`, figures.

    `mechanical_only` reports plumbing health without any accuracy, for prompt tuning on dev
    streams. Otherwise each run is summarized over the arms of its configuration (overrides
    for single streams do not widen the comparison) and gets a session-accuracy figure and a
    Pareto figure when at least one stream completed for all of those arms.
    """
    out.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, Any] = {}
    for run_dir in run_dirs:
        data = load_run(run_dir, ledger)
        if mechanical_only:
            metrics[run_dir.name] = mechanical_summary(data)
            continue
        config = load_manifest(run_dir).config
        streams = load_streams(config, streams_root) if config.task == "schemadrift" else None
        summary = summarize_run(data, arms=config.arms, streams=streams)
        metrics[run_dir.name] = summary
        if not summary["streams"]["analyzed"]:
            logger.warning("%s: no stream completed for all arms; no figures", run_dir.name)
            continue
        plot_session_accuracy(
            data,
            out / f"{run_dir.name}_session_accuracy.png",
            migrations=migration_sessions(config),
            arms=config.arms,
        )
        plot_pareto(summary, out / f"{run_dir.name}_pareto.png")
    (out / METRICS_FILE).write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s for %d runs", out / METRICS_FILE, len(metrics))
    return metrics


def run_gate(
    pilot_dir: Path, pilot_inj_dir: Path, *, out: Path, ledger: Path, streams_root: Path
) -> GateResult:
    """Evaluate the pilot gate on two finished runs and write `gate.json`.

    Generator soundness is checked on the streams of both runs.
    """
    pilot_streams = load_streams(load_manifest(pilot_dir).config, streams_root)
    inj_streams = load_streams(load_manifest(pilot_inj_dir).config, streams_root)
    problems = [
        f"{stream_id}: {problem}"
        for stream_id, stream in {**pilot_streams, **inj_streams}.items()
        for problem in validate_stream(stream)
    ]
    result = evaluate_gate(
        load_run(pilot_dir, ledger),
        load_run(pilot_inj_dir, ledger),
        streams=inj_streams,
        validation_problems=problems,
        cfg=GateConfig(),
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / GATE_FILE).write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return result


def load_streams(config: ExperimentConfig, streams_root: Path) -> dict[str, Stream]:
    """The SchemaDrift streams of `config`, by id."""
    return {stream_id: load_stream(streams_root / stream_id) for stream_id in config.stream_ids}


def migration_sessions(config: ExperimentConfig) -> tuple[int, ...]:
    """Sessions after which the database changes."""
    if config.task == "clbench":
        return (PRE_DRIFT_COUNT // config.questions_per_session,)
    return tuple(range(SESSIONS_PER_EPOCH, config.sessions, SESSIONS_PER_EPOCH))
