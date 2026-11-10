import logging
from pathlib import Path

import pytest
from fakes import FakeLLM, text_reply, tool_reply
from project_module.agent.context import ArmContext
from project_module.agent.online_arms import OracleArm, StatelessArm
from project_module.agent.probe import run_probes
from project_module.contracts import ToolSet
from project_module.memory.tools import MemoryReadTools
from project_module.prompts import load_prompt
from project_module.schemadrift.probes import make_probes

from .fake_env import FakeEnv, ScriptedArm, make_config, make_ctx, make_stream, question


def _writable_memory_tools(ctx: ArmContext) -> ToolSet:
    assert ctx.memory is not None
    return MemoryReadTools(ctx.memory, token_budget=500, writable_prefix="notes/")


def test_probe_runs_without_transcript_and_without_write_tools(tmp_path: Path) -> None:
    stream = make_stream(tmp_path / "stream")
    probes = make_probes(stream, 4, 2)
    client = FakeLLM(
        [
            tool_reply(("memory_search", {"pattern": "cust"})),
            tool_reply(("answer", {"answer": probes[0].answer})),
            text_reply("I do not know."),
        ]
    )
    ctx = make_ctx(tmp_path, client=client, env=FakeEnv([question("q1")]), arm="A5", stream=stream)

    results = run_probes(OracleArm(), ctx, 4)

    assert [(r.fact_id, r.injected) for r in results] == [(p.fact_id, p.injected) for p in probes]
    assert [r.correct for r in results] == [True, False]
    assert {(r.stream_id, r.arm, r.session) for r in results} == {(stream.stream_id, "A5", 4)}
    assert not ctx.layout.transcripts_dir(stream.stream_id, "A5").exists()
    for call in client.calls:
        names = {tool["function"]["name"] for tool in call["tools"]}
        assert names == {"memory_read", "memory_search", "answer"}
    assert {tag.component for tag in client.tags} == {"probe"}
    system = client.calls[0]["messages"][0]["content"]
    assert system.startswith(load_prompt("probe_system.md"))
    assert "cust_ref" in system


def test_probe_skips_arm_without_memory(tmp_path: Path) -> None:
    client = FakeLLM()
    ctx = make_ctx(
        tmp_path,
        client=client,
        env=FakeEnv([question("q1")]),
        arm="A0",
        stream=make_stream(tmp_path / "stream"),
        with_memory=False,
    )

    assert run_probes(StatelessArm(), ctx, 4) == []
    assert client.calls == []


def test_probe_refuses_tools_that_write_memory(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        client=FakeLLM(),
        env=FakeEnv([question("q1")]),
        stream=make_stream(tmp_path / "stream"),
    )

    with pytest.raises(ValueError, match="memory_write"):
        run_probes(ScriptedArm(tools=_writable_memory_tools), ctx, 4)


def test_probe_requires_a_stream(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, client=FakeLLM(), env=FakeEnv([question("q1")]))

    with pytest.raises(ValueError, match="stream"):
        run_probes(ScriptedArm(injected="memory"), ctx, 4)


def test_probe_uses_configured_count_and_truncated_layer(tmp_path: Path) -> None:
    client = FakeLLM()
    ctx = make_ctx(
        tmp_path,
        client=client,
        env=FakeEnv([question("q1")]),
        stream=make_stream(tmp_path / "stream"),
        config=make_config(injected_tokens=30),
    )
    long_memory = " ".join(f"rule-{i} holds." for i in range(200))

    results = run_probes(ScriptedArm(injected=long_memory), ctx, 8)

    assert len(results) == ctx.config.probes_per_checkpoint
    assert all(not r.correct for r in results)
    assert "[truncated]" in client.calls[0]["messages"][0]["content"]


def test_probe_forces_the_answer_tool_on_the_last_turn(tmp_path: Path) -> None:
    stream = make_stream(tmp_path / "stream")
    turns = make_config().budgets.online.max_turns
    script = [tool_reply(("memory_search", {"pattern": "x"})) for _ in range(turns - 1)]
    client = FakeLLM([*script, tool_reply(("answer", {"answer": "x"}))])
    ctx = make_ctx(tmp_path, client=client, env=FakeEnv([question("q1")]), arm="A5", stream=stream)
    config = ctx.config.model_copy(update={"probes_per_checkpoint": 1})
    ctx.config = config

    run_probes(OracleArm(), ctx, 4)

    assert [call["tool_choice"] for call in client.calls] == [None] * (turns - 1) + [
        {"type": "function", "name": "answer"}
    ]


def test_probe_info_logs_carry_no_scores(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    stream = make_stream(tmp_path / "stream")
    client = FakeLLM([tool_reply(("answer", {"answer": "x"})) for _ in range(2)])
    ctx = make_ctx(tmp_path, client=client, env=FakeEnv([question("q1")]), arm="A5", stream=stream)

    with caplog.at_level(logging.INFO, logger="project_module"):
        run_probes(OracleArm(), ctx, 4)

    info = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "probes asked" in info
    assert "correct" not in info
