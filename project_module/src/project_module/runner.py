"""Runs experiments: streams × arms as resumable jobs, sharing one cost ledger and stop signal.

A job is one arm on one stream. It runs the sessions in order and, after each one, writes the
session's rows atomically, snapshots the arm's memory and index, and only then records the
session as completed in `status.json`. A job that stopped part-way is resumed by discarding
the unfinished session and restoring the snapshot of the last completed one, so a resumed run
never repeats the calls of a completed session and never holds a row twice.
"""

import fcntl
import logging
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from project_module.agent.context import Arm, ArmContext, StopRequested, StopSignal
from project_module.agent.probe import run_probes
from project_module.agent.registry import arm_spec, make_arm
from project_module.agent.session import run_session
from project_module.clbench import (
    MANIFEST_NAME,
    MAX_QUERIES,
    MAX_RESULT_ROWS,
    SQL_TIMEOUT_S,
    CLBenchEnv,
)
from project_module.config import ArmName, ExperimentConfig, load_models
from project_module.contracts import LLMClient, RunStatus, TaskEnv
from project_module.evaluation.load import JobStatus, job_key
from project_module.layout import RunLayout
from project_module.llm.foundry import FoundryTransport
from project_module.llm.ledger import BudgetExceeded, CostLedger
from project_module.llm.metered import MeteredClient
from project_module.manifest import (
    RunManifest,
    config_sha256,
    file_sha256,
    git_state,
    load_manifest,
    matches_config,
    write_manifest,
)
from project_module.memory.store import MemoryStore
from project_module.retrieval import HybridIndex
from project_module.schemadrift.env import SchemaDriftEnv
from project_module.schemadrift.generator import STREAM_FILE, generate_stream, load_stream
from project_module.schemadrift.models import Stream
from project_module.schemadrift.validate import validate_stream

logger = logging.getLogger(__name__)

_SNAPSHOT_MEMORY: Final = "memory"
_SNAPSHOT_INDEX: Final = "index"
# Held (flock) by the process running a run directory, so two processes never share one.
RUN_LOCK_FILE: Final = ".run.lock"
# Libraries that log request URLs, which would put the endpoint host in the logs.
_URL_LOGGERS: Final = ("httpx", "httpx2", "httpcore", "httpcore2", "openai")


class RunLockedError(RuntimeError):
    """Another process is running the same run directory."""


class InvalidStreamError(ValueError):
    """A generated stream failed `validate_stream`; nothing was written."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} stream validation problem(s): " + "; ".join(problems))
        self.problems = list(problems)


# ---------------------------------------------------------------------------
# Setup helpers used by the command-line scripts
# ---------------------------------------------------------------------------


def configure_logging(level: int = logging.INFO) -> None:
    """Log to stderr, keeping HTTP client loggers quiet so no endpoint URL is written."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def build_client(config: ExperimentConfig, *, ledger: Path, models: Path) -> MeteredClient:
    """The metered Foundry client for `config`, reserving against the shared ledger.

    Raises
    ------
    ValueError
        If a model named in `config` is missing from the models file.
    RuntimeError
        If the endpoint or API key environment variable is not set.
    """
    specs = load_models(models)
    missing = {config.agent_model, config.consolidator_model, config.embedding_model} - set(specs)
    if missing:
        raise ValueError(f"models not defined in {models}: {sorted(missing)}")
    cost_ledger = CostLedger(
        ledger,
        phase=config.phase,
        phase_cap_usd=config.budgets.phase_cap_usd,
        hard_cap_usd=config.budgets.hard_cap_usd,
    )
    return MeteredClient(FoundryTransport.from_env(), cost_ledger, specs)


# ---------------------------------------------------------------------------
# Streams and environments
# ---------------------------------------------------------------------------


def generate_streams(config: ExperimentConfig, out_root: Path) -> list[Path]:
    """Generate and validate every stream of `config` into `out_root/<stream_id>/`.

    All streams are generated into a staging directory first; they are moved into place only
    when every one of them passes `validate_stream`, so a failure writes nothing.

    Raises
    ------
    ValueError
        If `config` is not a SchemaDrift experiment.
    FileExistsError
        If a stream directory already exists (streams are never overwritten).
    InvalidStreamError
        If a generated stream has validation problems.
    """
    if config.task != "schemadrift":
        raise ValueError(f"{config.name} is a {config.task} experiment; it has no streams")
    targets = [out_root / stream_id for stream_id in config.stream_ids]
    existing = [str(target) for target in targets if target.exists()]
    if existing:
        raise FileExistsError(f"stream directories already exist: {existing}")
    out_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_root, prefix=".staging-") as staging:
        problems: list[str] = []
        for stream_id, seed in zip(config.stream_ids, config.seeds, strict=True):
            stream_dir = Path(staging) / stream_id
            generate_stream(
                stream_id,
                seed,
                stream_dir,
                sessions=config.sessions,
                questions_per_session=config.questions_per_session,
                inject=config.inject_false_notes,
                drift=config.drift,
            )
            found = validate_stream(load_stream(stream_dir))
            problems += [f"{stream_id}: {problem}" for problem in found]
            logger.info("stream %s (seed %d): %d problems", stream_id, seed, len(found))
        if problems:
            raise InvalidStreamError(problems)
        for stream_id, target in zip(config.stream_ids, targets, strict=True):
            (Path(staging) / stream_id).rename(target)
    return targets


def make_env(
    config: ExperimentConfig, stream_id: str, seed: int, *, streams_root: Path, clbench_root: Path
) -> tuple[TaskEnv, Stream | None]:
    """The task environment of one stream, and its SchemaDrift definition if it has one.

    Raises
    ------
    ValueError
        If the stream on disk does not match `config` (id, seed, sessions, questions per
        session, presence of false notes, or drift), or a CL-Bench config states other query, row
        or timeout limits than the CL-Bench adapter enforces.
    """
    if config.task == "clbench":
        env = CLBenchEnv(
            clbench_root, run_index=seed, questions_per_session=config.questions_per_session
        )
        _require_match(stream_id, "stream id", env.env_id, stream_id)
        _require_match(stream_id, "sessions", env.sessions, config.sessions)
        # The adapter enforces CL-Bench's own limits; the config must state the same ones.
        online = config.budgets.online
        _require_match(stream_id, "max_queries", MAX_QUERIES, online.max_queries)
        _require_match(stream_id, "sql_row_limit", MAX_RESULT_ROWS, online.sql_row_limit)
        _require_match(stream_id, "sql_timeout_s", SQL_TIMEOUT_S, online.sql_timeout_s)
        return env, None
    stream = load_stream(streams_root / stream_id)
    _require_match(stream_id, "stream id", stream.stream_id, stream_id)
    _require_match(stream_id, "seed", stream.seed, seed)
    _require_match(stream_id, "sessions", stream.sessions, config.sessions)
    _require_match(
        stream_id,
        "questions per session",
        stream.questions_per_session,
        config.questions_per_session,
    )
    _require_match(stream_id, "false notes", bool(stream.injections), config.inject_false_notes)
    _require_match(stream_id, "drift", stream.drift, config.drift)
    online = config.budgets.online
    schemadrift = SchemaDriftEnv(
        stream,
        row_limit=online.sql_row_limit,
        timeout_s=online.sql_timeout_s,
        max_queries=online.max_queries,
        feedback=config.feedback == "graded",
    )
    return schemadrift, stream


def _require_match(stream_id: str, what: str, found: object, expected: object) -> None:
    if found != expected:
        raise ValueError(
            f"stream {stream_id}: {what} is {found!r}, the config expects {expected!r}"
        )


def _input_file(
    config: ExperimentConfig, stream_id: str, streams_root: Path, clbench_root: Path
) -> Path:
    if config.task == "clbench":
        return clbench_root / MANIFEST_NAME
    return streams_root / stream_id / STREAM_FILE


# ---------------------------------------------------------------------------
# One job: one arm on one stream
# ---------------------------------------------------------------------------


def read_job_status(layout: RunLayout, stream_id: str, arm: ArmName) -> JobStatus | None:
    """The job's `status.json`, or None if the job has never started."""
    path = layout.status_file(stream_id, arm)
    if not path.is_file():
        return None
    return JobStatus.model_validate_json(path.read_text(encoding="utf-8"))


def run_stream_arm(
    config: ExperimentConfig,
    env: TaskEnv,
    stream: Stream | None,
    arm_name: ArmName,
    *,
    layout: RunLayout,
    run_id: str,
    client: LLMClient,
    stop: StopSignal,
) -> RunStatus:
    """Run, or resume, the sessions of `arm_name` on the stream of `env`.

    Each session runs the questions, injects the false notes due after it (when the config
    injects; before or after the consolidation per `false_note_timing`), lets the arm
    consolidate, asks the probes (SchemaDrift checkpoints only), then
    writes the session's rows, snapshots memory and index, and updates `status.json`. A
    completed job returns at once. `stop` is checked when the job starts, before every
    session and before every question; a stopped job records the stop's reason as its status
    and keeps only its completed sessions. `BudgetExceeded` requests a stop. Any other
    exception fails the job and is logged.
    """
    stream_id = env.env_id
    previous = read_job_status(layout, stream_id, arm_name)
    if previous is not None and previous.status == "completed":
        logger.info("%s/%s: already completed", stream_id, arm_name)
        return "completed"
    completed = 0 if previous is None else previous.completed_sessions
    if stop.is_set():
        return _record_status(layout, stream_id, arm_name, stop.reason, completed)
    try:
        ctx = _prepare_job(
            config,
            env,
            stream,
            arm_name,
            layout=layout,
            run_id=run_id,
            client=client,
            completed=completed,
            stop=stop,
        )
        arm = make_arm(arm_name)
        arm.setup(ctx)
        _record_status(layout, stream_id, arm_name, "running", completed)
        for session in range(completed + 1, env.sessions + 1):
            if stop.is_set():
                return _record_status(layout, stream_id, arm_name, stop.reason, completed)
            _run_one_session(arm, ctx, session)
            completed = session
            _record_status(layout, stream_id, arm_name, "running", completed)
    except BudgetExceeded as error:
        logger.warning(
            "%s/%s: budget exhausted (%s); stopping all jobs", stream_id, arm_name, error
        )
        stop.request("budget_exhausted")
        return _record_status(layout, stream_id, arm_name, "budget_exhausted", completed)
    except StopRequested:
        logger.info("%s/%s: stopped in session %d", stream_id, arm_name, completed + 1)
        return _record_status(layout, stream_id, arm_name, stop.reason, completed)
    except Exception:
        logger.exception("%s/%s: job failed in session %d", stream_id, arm_name, completed + 1)
        return _record_status(layout, stream_id, arm_name, "failed", completed)
    return _record_status(layout, stream_id, arm_name, "completed", completed)


def _prepare_job(
    config: ExperimentConfig,
    env: TaskEnv,
    stream: Stream | None,
    arm_name: ArmName,
    *,
    layout: RunLayout,
    run_id: str,
    client: LLMClient,
    completed: int,
    stop: StopSignal,
) -> ArmContext:
    """Discard every trace of unfinished sessions and rebuild the state after `completed`."""
    stream_id = env.env_id
    for session in range(completed + 1, env.sessions + 1):
        for path in (
            layout.transcript_file(stream_id, arm_name, session),
            layout.results_file(stream_id, arm_name, session),
            layout.probes_file(stream_id, arm_name, session),
            layout.consolidation_file(stream_id, arm_name, session),
        ):
            path.unlink(missing_ok=True)
        _replace_dir(layout.snapshot_dir(stream_id, arm_name, session), None)
    snapshot = layout.snapshot_dir(stream_id, arm_name, completed) if completed else None
    spec = arm_spec(arm_name)
    memory = None
    if spec.keeps_memory:
        memory = MemoryStore(layout.memory_dir(stream_id, arm_name), writable=True)
        _restore_memory(memory, None if snapshot is None else snapshot / _SNAPSHOT_MEMORY)
    index = None
    if spec.uses_index:
        index_dir = layout.index_dir(stream_id, arm_name)
        _replace_dir(index_dir, None if snapshot is None else snapshot / _SNAPSHOT_INDEX)
        index = HybridIndex(index_dir, client, config.embedding_model)
    if completed:
        logger.info("%s/%s: resuming after session %d", stream_id, arm_name, completed)
    return ArmContext(
        env=env,
        stream=stream,
        config=config,
        client=client,
        layout=layout,
        run_id=run_id,
        stream_id=stream_id,
        memory=memory,
        transcripts=MemoryStore(layout.transcripts_dir(stream_id, arm_name), writable=False),
        index=index,
        stop=stop,
    )


def _inject_due_notes(arm: Arm, ctx: ArmContext, session: int, *, expose: bool) -> None:
    if not ctx.config.inject_false_notes or ctx.stream is None:
        return
    for note in ctx.stream.injections:
        if note.after_session == session:
            arm.inject_false_note(ctx, note, expose=expose)


def _run_one_session(arm: Arm, ctx: ArmContext, session: int) -> None:
    layout, stream_id = ctx.layout, ctx.stream_id
    results = run_session(arm, ctx, session)
    after = ctx.config.false_note_timing == "after_consolidation"
    if not after:
        _inject_due_notes(arm, ctx, session, expose=False)
    record = arm.after_session(ctx, session)
    if after:
        _inject_due_notes(arm, ctx, session, expose=True)
    probe_due = ctx.stream is not None and session in ctx.config.probe_sessions
    probes = run_probes(arm, ctx, session) if probe_due else None

    _write_rows(layout.results_file(stream_id, arm.name, session), results)
    if probes is not None:
        _write_rows(layout.probes_file(stream_id, arm.name, session), probes)
    if record is not None:
        _write_rows(layout.consolidation_file(stream_id, arm.name, session), [record])
    snapshot = layout.snapshot_dir(stream_id, arm.name, session)
    if ctx.memory is not None:
        ctx.memory.snapshot(snapshot / _SNAPSHOT_MEMORY)
    if ctx.index is not None:
        _replace_dir(snapshot / _SNAPSHOT_INDEX, layout.index_dir(stream_id, arm.name))
    # Grades stay out of INFO logs: dev runs are tuned on mechanical signals only.
    logger.info(
        "%s/%s: session %d done, %d questions, %d tool errors in %d calls",
        stream_id,
        arm.name,
        session,
        len(results),
        sum(result.tool_errors for result in results),
        sum(result.tool_calls for result in results),
    )
    logger.debug(
        "%s/%s: session %d, %d/%d correct",
        stream_id,
        arm.name,
        session,
        sum(result.correct for result in results),
        len(results),
    )


def _restore_memory(memory: MemoryStore, snapshot: Path | None) -> None:
    if snapshot is not None:
        memory.restore(snapshot)
        return
    with tempfile.TemporaryDirectory() as empty:
        memory.restore(Path(empty))


def _replace_dir(dest: Path, src: Path | None) -> None:
    """Make `dest` a copy of `src`, or remove it when `src` is None."""
    if dest.exists():
        shutil.rmtree(dest)
    if src is not None:
        shutil.copytree(src, dest)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_rows(path: Path, rows: Sequence[BaseModel]) -> None:
    _write_atomic(path, "".join(row.model_dump_json() + "\n" for row in rows))


def _record_status(
    layout: RunLayout, stream_id: str, arm: ArmName, status: RunStatus, completed: int
) -> RunStatus:
    record = JobStatus(status=status, completed_sessions=completed)
    _write_atomic(layout.status_file(stream_id, arm), record.model_dump_json() + "\n")
    if status != "running":
        logger.info("%s/%s: %s after %d sessions", stream_id, arm, status, completed)
    return status


def _mark_not_started(
    layout: RunLayout, stream_id: str, arm: ArmName, reason: RunStatus
) -> RunStatus:
    """Record a job that was never started because the run stopped, unless completed."""
    previous = read_job_status(layout, stream_id, arm)
    if previous is not None and previous.status == "completed":
        return "completed"
    completed = 0 if previous is None else previous.completed_sessions
    return _record_status(layout, stream_id, arm, reason, completed)


# ---------------------------------------------------------------------------
# A whole experiment
# ---------------------------------------------------------------------------


def run_experiment(
    config: ExperimentConfig,
    *,
    out_root: Path,
    streams_root: Path,
    clbench_root: Path,
    client: LLMClient,
    resume: Path | None = None,
) -> Path:
    """Run every job of `config` and return the run directory.

    A new run lives in `out_root/<name>-<UTC timestamp>` and starts with `manifest.json`.
    With `resume`, that run directory is continued: its manifest must match `config` and the
    stream files, and every job that is not completed (failed ones included) picks up after its
    last completed session. Jobs are submitted stream by stream; before each stream the runner
    waits for a free worker and does not submit it once the run is stopping.

    The process holds an exclusive lock on the run directory while it runs. On an interrupt
    (Ctrl-C) or any other exception, every job is asked to stop before its next question,
    jobs not yet started are cancelled and recorded as `interrupted`, and the exception is
    re-raised once the running jobs have recorded their status.

    Every stream is loaded and checked against `config` before the run directory is created.

    Raises
    ------
    ValueError
        If a stream does not match `config`, or `resume` was started with a different
        configuration or different stream files.
    RunLockedError
        If another process is running the same run directory.
    """
    inputs = {
        stream_id: file_sha256(_input_file(config, stream_id, streams_root, clbench_root))
        for stream_id in config.stream_ids
    }
    envs = [
        make_env(config, stream_id, seed, streams_root=streams_root, clbench_root=clbench_root)
        for stream_id, seed in zip(config.stream_ids, config.seeds, strict=True)
    ]
    run_dir = _start_run(config, out_root, inputs) if resume is None else resume
    with run_lock(run_dir):
        if resume is not None:
            _check_resumable(resume, config, inputs)
        layout = RunLayout(run_dir)
        statuses = _run_jobs(config, envs, layout=layout, run_id=run_dir.name, client=client)
    logger.info("run %s finished: %s", run_dir.name, dict(Counter(statuses.values())))
    return run_dir


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on `run_dir` for the duration of the block.

    The lock is an `flock` on `RUN_LOCK_FILE`, so the operating system releases it when the
    process dies, and a crashed run never blocks a later resume.

    Raises
    ------
    RunLockedError
        If another holder has the lock.
    """
    with (run_dir / RUN_LOCK_FILE).open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunLockedError(f"{run_dir} is being run by another process") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _run_jobs(
    config: ExperimentConfig,
    envs: Sequence[tuple[TaskEnv, Stream | None]],
    *,
    layout: RunLayout,
    run_id: str,
    client: LLMClient,
) -> dict[str, RunStatus]:
    stop = StopSignal()
    statuses: dict[str, RunStatus] = {}
    pending: dict[Future[RunStatus], tuple[str, ArmName]] = {}
    submitted: dict[tuple[str, ArmName], Future[RunStatus]] = {}
    pool = ThreadPoolExecutor(max_workers=config.concurrency)
    try:
        for position, (stream_id, (env, stream)) in enumerate(
            zip(config.stream_ids, envs, strict=True)
        ):
            _wait_below(pending, config.concurrency, statuses)
            if stop.is_set():
                remaining = config.stream_ids[position:]
                logger.warning("run stopping (%s); streams not started: %s", stop.reason, remaining)
                for skipped in remaining:
                    for arm in config.arms_for(skipped):
                        statuses[job_key(skipped, arm)] = _mark_not_started(
                            layout, skipped, arm, stop.reason
                        )
                break
            for arm in config.arms_for(stream_id):
                future = pool.submit(
                    run_stream_arm,
                    config,
                    env,
                    stream,
                    arm,
                    layout=layout,
                    run_id=run_id,
                    client=client,
                    stop=stop,
                )
                pending[future] = (stream_id, arm)
                submitted[(stream_id, arm)] = future
        _wait_below(pending, 1, statuses)
    except BaseException:
        logger.warning("run interrupted; asking every job to stop before its next question")
        stop.request("interrupted")
        pool.shutdown(wait=True, cancel_futures=True)
        for stream_id in config.stream_ids:
            for arm in config.arms_for(stream_id):
                future = submitted.get((stream_id, arm))
                if future is None or future.cancelled():
                    _mark_not_started(layout, stream_id, arm, "interrupted")
        raise
    pool.shutdown()
    return statuses


def incomplete_jobs(config: ExperimentConfig, run_dir: Path) -> dict[str, RunStatus]:
    """Jobs of `config` in `run_dir` that are not completed, with their status."""
    layout = RunLayout(run_dir)
    incomplete: dict[str, RunStatus] = {}
    for stream_id in config.stream_ids:
        for arm in config.arms_for(stream_id):
            status = read_job_status(layout, stream_id, arm)
            if status is None or status.status != "completed":
                incomplete[job_key(stream_id, arm)] = "running" if status is None else status.status
    return incomplete


def _wait_below(
    pending: dict[Future[RunStatus], tuple[str, ArmName]],
    limit: int,
    statuses: dict[str, RunStatus],
) -> None:
    """Collect finished jobs until fewer than `limit` are pending."""
    while len(pending) >= limit and pending:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            statuses[job_key(*pending.pop(future))] = future.result()


def _start_run(config: ExperimentConfig, out_root: Path, inputs: dict[str, str]) -> Path:
    run_id = f"{config.name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    git_sha, git_dirty = git_state()
    write_manifest(
        RunLayout(run_dir),
        RunManifest(
            run_id=run_id,
            phase=config.phase,
            created_utc=datetime.now(UTC).isoformat(timespec="seconds"),
            git_sha=git_sha,
            git_dirty=git_dirty,
            config_sha256=config_sha256(config),
            config=config,
            input_sha256=inputs,
        ),
    )
    logger.info("run %s started (phase %s)", run_id, config.phase)
    return run_dir


def _check_resumable(run_dir: Path, config: ExperimentConfig, inputs: dict[str, str]) -> None:
    manifest = load_manifest(run_dir)
    if not matches_config(manifest.config_sha256, config):
        raise ValueError(f"{run_dir} was started with a different configuration")
    if manifest.input_sha256 != inputs:
        raise ValueError(f"the stream files of {run_dir} have changed since it started")
    logger.info("resuming run %s", run_dir.name)
