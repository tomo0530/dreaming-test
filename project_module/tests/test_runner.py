import importlib.util
import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fakes import FakeLLM, FakeTransport, text_reply, tool_reply
from project_module.agent.context import StopSignal
from project_module.config import ArmName, Budgets, ExperimentConfig, OnlineBudget, load_models
from project_module.contracts import ChatResult, CostTag, QuestionResult, ToolChoice
from project_module.layout import RunLayout
from project_module.llm.ledger import CostLedger
from project_module.llm.metered import MeteredClient
from project_module.manifest import load_manifest
from project_module.memory.store import MemoryStore, false_notes_present
from project_module.runner import (
    InvalidStreamError,
    RunLockedError,
    build_client,
    generate_streams,
    incomplete_jobs,
    make_env,
    read_job_status,
    run_experiment,
    run_lock,
    run_stream_arm,
)

from .agent.fake_env import MARKER, STREAM_ID, FakeEnv, make_stream, question

REPO = Path(__file__).resolve().parents[2]
MODELS = REPO / "configs" / "models.yaml"
RUN_ID = "run-test"

Observer = Callable[[CostTag, list[dict[str, Any]]], None]


class RunnerLLM(FakeLLM):
    """Gives no answers, writes `summary.md` in each consolidation, and can fail once.

    `observe` sees every chat call before it is answered; `fail_when` picks the one call that
    raises instead.
    """

    def __init__(
        self,
        *,
        observe: Observer | None = None,
        fail_when: Callable[[CostTag, list[dict[str, Any]]], bool] | None = None,
    ) -> None:
        super().__init__()
        self._observe = observe
        self._fail_when = fail_when
        self.users: list[str] = []

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str,
        max_completion_tokens: int,
        tag: CostTag,
        tool_choice: ToolChoice | None = None,
    ) -> ChatResult:
        self.tags.append(tag)
        self.users.append(_user(messages))
        if self._observe is not None:
            self._observe(tag, messages)
        if self._fail_when is not None and self._fail_when(tag, messages):
            self._fail_when = None
            raise RuntimeError("injected failure")
        first_turn = not any(message["role"] == "assistant" for message in messages)
        if tag.component == "consolidation" and first_turn:
            content = f"summary after session {tag.session}\n"
            return tool_reply(("write_file", {"path": "memory/summary.md", "content": content}))
        return text_reply("I cannot answer.")


def _user(messages: list[dict[str, Any]]) -> str:
    return str(next(m["content"] for m in messages if m["role"] == "user"))


def _config(**overrides: Any) -> ExperimentConfig:
    fields: dict[str, Any] = {
        "name": "runner-test",
        "phase": "test",
        "arms": ["A1"],
        "stream_ids": [STREAM_ID],
        "seeds": [7],
        "sessions": 4,
        "questions_per_session": 2,
        "inject_false_notes": False,
        "agent_model": "gpt-6-luna",
        "consolidator_model": "gpt-6-luna",
        "embedding_model": "text-embedding-3-large",
        "probe_sessions": [],
        "probes_per_checkpoint": 2,
        "concurrency": 1,
        "budgets": Budgets(phase_cap_usd=1.0, online=OnlineBudget(memory_read_tokens=500)),
    }
    return ExperimentConfig(**(fields | overrides))


def _env(sessions: int = 4) -> FakeEnv:
    env = FakeEnv([question(f"q{s}{c}", s) for s in range(1, sessions + 1) for c in "ab"])
    env.env_id = STREAM_ID
    return env


def _run_job(
    config: ExperimentConfig,
    env: FakeEnv,
    arm: ArmName,
    layout: RunLayout,
    client: FakeLLM,
    *,
    stream_root: Path | None = None,
    stop: StopSignal | None = None,
) -> str:
    return run_stream_arm(
        config,
        env,
        None if stream_root is None else make_stream(stream_root),
        arm,
        layout=layout,
        run_id=RUN_ID,
        client=client,
        stop=stop or StopSignal(),
    )


def _rows(layout: RunLayout, arm: ArmName) -> list[QuestionResult]:
    results_dir = layout.results_file(STREAM_ID, arm, 1).parent
    return [
        QuestionResult.model_validate_json(line)
        for path in sorted(results_dir.glob("session_*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def _small_config(
    name: str, stream_ids: list[str], seeds: list[int], **overrides: Any
) -> ExperimentConfig:
    return _config(
        name=name,
        arms=["A0"],
        stream_ids=stream_ids,
        seeds=seeds,
        sessions=2,
        questions_per_session=3,
        **overrides,
    )


def test_resume_skips_completed_sessions(tmp_path: Path) -> None:
    config, env, layout = _config(), _env(), RunLayout(tmp_path / "run")
    failing = RunnerLLM(fail_when=lambda tag, messages: "(q3b)" in _user(messages))

    assert _run_job(config, env, "A1", layout, failing) == "failed"
    status = read_job_status(layout, STREAM_ID, "A1")
    assert status is not None and status.completed_sessions == 2

    resumed = RunnerLLM()
    assert _run_job(config, env, "A1", layout, resumed) == "completed"
    assert {tag.session for tag in resumed.tags} == {3, 4}
    assert "(q3a)" in resumed.users[0]

    again = RunnerLLM()
    assert _run_job(config, env, "A1", layout, again) == "completed"
    assert again.tags == []


@pytest.mark.parametrize("arm", ["A1", "A2"])
def test_resume_restores_state_and_has_no_duplicate_rows(tmp_path: Path, arm: ArmName) -> None:
    config, env, layout = _config(probe_sessions=[3]), _env(), RunLayout(tmp_path / "run")
    memory_dir = layout.memory_dir(STREAM_ID, arm)
    index_ids = layout.index_dir(STREAM_ID, arm) / "ids.json"

    def state() -> object:
        if arm == "A1":
            return (memory_dir / "summary.md").read_text()
        return sorted({chunk.split("-")[0] for chunk in json.loads(index_ids.read_text())})

    failing = RunnerLLM(fail_when=lambda tag, _: tag.component == "probe" and tag.session == 3)
    assert _run_job(config, env, arm, layout, failing, stream_root=tmp_path / "s") == "failed"
    # The failure hit after the session's transcript, consolidation and indexing.
    assert layout.transcript_file(STREAM_ID, arm, 3).is_file()
    assert state() == ("summary after session 3\n" if arm == "A1" else ["s1", "s2", "s3"])
    assert not layout.results_file(STREAM_ID, arm, 3).exists()

    seen: dict[int, object] = {}

    def observe(tag: CostTag, _: list[dict[str, Any]]) -> None:
        seen.setdefault(tag.session, state())

    resumed = RunnerLLM(observe=observe)
    assert _run_job(config, env, arm, layout, resumed, stream_root=tmp_path / "s") == "completed"

    assert seen[3] == ("summary after session 2\n" if arm == "A1" else ["s1", "s2"])
    assert layout.transcript_file(STREAM_ID, arm, 3).read_text().count("(q3a)") == 1
    qids = [row.qid for row in _rows(layout, arm)]
    assert sorted(qids) == sorted(f"q{s}{c}" for s in range(1, 5) for c in "ab")
    probes = layout.probes_file(STREAM_ID, arm, 3).read_text().splitlines()
    assert len(probes) == config.probes_per_checkpoint


def test_injection_happens_before_consolidation(tmp_path: Path) -> None:
    config = _config(arms=["A4"], inject_false_notes=True)
    layout = RunLayout(tmp_path / "run")
    memory = MemoryStore(layout.memory_dir(STREAM_ID, "A4"), writable=True)
    notes = [make_stream(tmp_path / "s").injections[0]]
    at_consolidation: dict[int, list[str]] = {}

    def observe(tag: CostTag, _: list[dict[str, Any]]) -> None:
        if tag.component == "consolidation":
            at_consolidation.setdefault(tag.session, false_notes_present(memory, notes))

    client = RunnerLLM(observe=observe)
    assert _run_job(config, _env(), "A4", layout, client, stream_root=tmp_path / "s") == "completed"

    assert at_consolidation == {1: [], 2: [], 3: [MARKER]}
    assert false_notes_present(memory, notes) == [MARKER]


def test_injection_after_consolidation_reaches_the_next_session_first(tmp_path: Path) -> None:
    config = _config(arms=["A4"], inject_false_notes=True, false_note_timing="after_consolidation")
    layout = RunLayout(tmp_path / "run")
    memory = MemoryStore(layout.memory_dir(STREAM_ID, "A4"), writable=True)
    notes = [make_stream(tmp_path / "s").injections[0]]
    at_consolidation: dict[int, list[str]] = {}

    def observe(tag: CostTag, _: list[dict[str, Any]]) -> None:
        if tag.component == "consolidation":
            at_consolidation.setdefault(tag.session, false_notes_present(memory, notes))

    client = RunnerLLM(observe=observe)
    assert _run_job(config, _env(), "A4", layout, client, stream_root=tmp_path / "s") == "completed"

    assert at_consolidation == {1: [], 2: [], 3: []}
    snapshot = MemoryStore(layout.snapshot_dir(STREAM_ID, "A4", 3) / "memory", writable=False)
    assert MARKER in snapshot.read_all("injected.md")


def test_stop_set_before_start_records_budget_exhausted_without_calls(tmp_path: Path) -> None:
    layout, stop, client = RunLayout(tmp_path / "run"), StopSignal(), RunnerLLM()
    stop.request("budget_exhausted")

    assert _run_job(_config(), _env(), "A1", layout, client, stop=stop) == "budget_exhausted"
    assert client.tags == []
    status = read_job_status(layout, STREAM_ID, "A1")
    assert status is not None and (status.status, status.completed_sessions) == (
        "budget_exhausted",
        0,
    )


def test_budget_exhaustion_stops_new_streams(tmp_path: Path) -> None:
    config = _small_config("budget", ["b-01", "b-02", "b-03"], [20, 21, 23])
    generate_streams(config, tmp_path / "streams")
    ledger = CostLedger(
        tmp_path / "ledger.sqlite", phase="test", phase_cap_usd=0.02, hard_cap_usd=200.0
    )
    # Each answer bills 50k input tokens, so the cap is hit within the first stream.
    transport = FakeTransport([text_reply("no", input_tokens=50_000) for _ in range(100)])
    client = MeteredClient(transport, ledger, load_models(MODELS))

    run_dir = run_experiment(
        config,
        out_root=tmp_path / "outputs",
        streams_root=tmp_path / "streams",
        clbench_root=tmp_path / "none",
        client=client,
    )

    layout = RunLayout(run_dir)
    assert incomplete_jobs(config, run_dir) == {
        "b-01/A0": "budget_exhausted",
        "b-02/A0": "budget_exhausted",
        "b-03/A0": "budget_exhausted",
    }
    # The first stream stopped part-way and no other stream sent a call.
    assert 0 < len(transport.calls) < config.sessions * config.questions_per_session
    for stream_id in ("b-02", "b-03"):
        assert not layout.transcripts_dir(stream_id, "A0").exists()
    assert ledger.spent_usd(phase="test") <= 0.02


def test_arm_overrides_create_expected_jobs(tmp_path: Path) -> None:
    config = _small_config(
        "overrides", ["o-01", "o-02"], [20, 21], arm_overrides={"o-01": ["A0", "A2", "A5"]}
    )
    generate_streams(config, tmp_path / "streams")

    run_dir = run_experiment(
        config,
        out_root=tmp_path / "outputs",
        streams_root=tmp_path / "streams",
        clbench_root=tmp_path / "none",
        client=RunnerLLM(),
    )

    jobs = {(path.parent.name, path.name) for path in run_dir.glob("*/A*") if path.is_dir()}
    assert jobs == {("o-01", "A0"), ("o-01", "A2"), ("o-01", "A5"), ("o-02", "A0")}
    assert incomplete_jobs(config, run_dir) == {}
    manifest = load_manifest(run_dir)
    assert manifest.run_id == run_dir.name and manifest.config == config
    assert set(manifest.input_sha256) == {"o-01", "o-02"}


def test_resume_reruns_failed_jobs_only_and_refuses_other_configs(tmp_path: Path) -> None:
    config = _small_config("resume", ["r-01", "r-02"], [20, 21])
    generate_streams(config, tmp_path / "streams")
    kwargs: dict[str, Any] = {
        "out_root": tmp_path / "outputs",
        "streams_root": tmp_path / "streams",
        "clbench_root": tmp_path / "none",
    }
    failing = RunnerLLM(fail_when=lambda tag, _: tag.stream_id == "r-02")
    run_dir = run_experiment(config, client=failing, **kwargs)
    assert incomplete_jobs(config, run_dir) == {"r-02/A0": "failed"}

    resumed = RunnerLLM()
    assert run_experiment(config, client=resumed, resume=run_dir, **kwargs) == run_dir
    assert incomplete_jobs(config, run_dir) == {}
    assert {tag.stream_id for tag in resumed.tags} == {"r-02"}
    assert {tag.run_id for tag in failing.tags + resumed.tags} == {run_dir.name}

    with pytest.raises(ValueError, match="different configuration"):
        run_experiment(
            config.model_copy(update={"concurrency": 2}),
            client=RunnerLLM(),
            resume=run_dir,
            **kwargs,
        )


def test_make_env_rejects_a_stream_that_does_not_match_the_config(tmp_path: Path) -> None:
    config = _small_config("env", ["e-01"], [20])
    generate_streams(config, tmp_path / "streams")
    env, stream = make_env(
        config, "e-01", 20, streams_root=tmp_path / "streams", clbench_root=tmp_path / "none"
    )
    assert stream is not None and env.env_id == "e-01" and env.sessions == 2

    for changed in ({"sessions": 3}, {"seeds": [21]}, {"inject_false_notes": True}):
        wrong = config.model_copy(update=changed)
        with pytest.raises(ValueError, match="e-01"):
            make_env(
                wrong,
                "e-01",
                wrong.seeds[0],
                streams_root=tmp_path / "streams",
                clbench_root=tmp_path / "none",
            )


def test_generate_streams_refuses_invalid_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _small_config("gen", ["g-01", "g-02"], [20, 21])
    out = tmp_path / "streams"
    monkeypatch.setattr(
        "project_module.runner.validate_stream",
        lambda stream: ["broken question"] if stream.stream_id == "g-02" else [],
    )

    with pytest.raises(InvalidStreamError) as raised:
        generate_streams(config, out)
    assert raised.value.problems == ["g-02: broken question"]
    assert list(out.iterdir()) == []

    config_file = tmp_path / "gen.yaml"
    config_file.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    spec = importlib.util.spec_from_file_location(
        "generate_streams_cli", REPO / "scripts" / "generate_streams.py"
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["--config", str(config_file), "--out", str(out)]) == 1
    assert list(out.iterdir()) == []


def test_generate_streams_never_overwrites(tmp_path: Path) -> None:
    config = _small_config("gen", ["g-01"], [20])
    generate_streams(config, tmp_path / "streams")
    with pytest.raises(FileExistsError):
        generate_streams(config, tmp_path / "streams")


def test_build_client_rejects_models_missing_from_the_models_file(tmp_path: Path) -> None:
    config = _config(agent_model="no-such-model")
    with pytest.raises(ValueError, match="no-such-model"):
        build_client(config, ledger=tmp_path / "ledger.sqlite", models=MODELS)


def test_stop_between_questions_keeps_only_completed_sessions(tmp_path: Path) -> None:
    layout, stop = RunLayout(tmp_path / "run"), StopSignal()

    def observe(tag: CostTag, messages: list[dict[str, Any]]) -> None:
        if tag.session == 2 and tag.component == "online":
            stop.request("interrupted")

    client = RunnerLLM(observe=observe)
    assert _run_job(_config(), _env(), "A1", layout, client, stop=stop) == "interrupted"

    status = read_job_status(layout, STREAM_ID, "A1")
    assert status is not None and (status.status, status.completed_sessions) == ("interrupted", 1)
    # The second question of session 2 was never asked.
    assert any("(q2a)" in user for user in client.users)
    assert not any("(q2b)" in user for user in client.users)
    resumed = RunnerLLM()
    assert _run_job(_config(), _env(), "A1", layout, resumed) == "completed"
    assert {tag.session for tag in resumed.tags} == {2, 3, 4}


def test_session_logs_at_info_carry_no_grades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="project_module"):
        _run_job(_config(arms=["A0"]), _env(), "A0", RunLayout(tmp_path / "run"), RunnerLLM())

    info = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "session 1 done" in info
    for grade in ("correct", "no_submission", "wrong_result"):
        assert grade not in info


def test_run_refuses_a_run_directory_locked_by_another_process(tmp_path: Path) -> None:
    config = _small_config("lock", ["l-01"], [20])
    generate_streams(config, tmp_path / "streams")
    kwargs: dict[str, Any] = {
        "out_root": tmp_path / "outputs",
        "streams_root": tmp_path / "streams",
        "clbench_root": tmp_path / "none",
    }
    run_dir = run_experiment(config, client=RunnerLLM(), **kwargs)
    blocked = RunnerLLM()

    with run_lock(run_dir), pytest.raises(RunLockedError):
        run_experiment(config, client=blocked, resume=run_dir, **kwargs)

    assert blocked.tags == []
    # Released on exit: the run can be resumed again.
    assert run_experiment(config, client=RunnerLLM(), resume=run_dir, **kwargs) == run_dir


def test_interrupt_stops_jobs_records_interrupted_and_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _small_config("ctrl-c", ["c-01", "c-02"], [20, 21])
    generate_streams(config, tmp_path / "streams")
    started, stopped = threading.Event(), threading.Event()

    class _Signal(StopSignal):
        def request(self, reason: Any) -> None:
            super().request(reason)
            stopped.set()

    def observe(tag: CostTag, messages: list[dict[str, Any]]) -> None:
        started.set()
        assert stopped.wait(timeout=10)

    def interrupted_wait(*args: Any, **kwargs: Any) -> Any:
        assert started.wait(timeout=10)
        raise KeyboardInterrupt

    monkeypatch.setattr("project_module.runner.StopSignal", _Signal)
    monkeypatch.setattr("project_module.runner.wait", interrupted_wait)
    client = RunnerLLM(observe=observe)

    with pytest.raises(KeyboardInterrupt):
        run_experiment(
            config,
            out_root=tmp_path / "outputs",
            streams_root=tmp_path / "streams",
            clbench_root=tmp_path / "none",
            client=client,
        )

    [run_dir] = list((tmp_path / "outputs").iterdir())
    assert incomplete_jobs(config, run_dir) == {
        "c-01/A0": "interrupted",
        "c-02/A0": "interrupted",
    }
    # Only the first question of the running job was asked; the other stream never started.
    assert len(client.tags) == 1 and client.tags[0].stream_id == "c-01"
    with run_lock(run_dir):
        pass
