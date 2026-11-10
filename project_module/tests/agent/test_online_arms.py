from pathlib import Path

import pytest
from fakes import FakeLLM
from project_module.agent.online_arms import (
    FACTS_FILE,
    INJECTED_FILE,
    OracleArm,
    RawRagArm,
    StatelessArm,
)
from project_module.contracts import ToolCall, ToolSet
from project_module.schemadrift.models import Fact, InjectedNote
from project_module.tokens import count_tokens
from project_module.transcript import TranscriptWriter

from .fake_env import STREAM_ID, FakeEnv, make_config, make_ctx, make_stream, question

NOTE = InjectedNote(
    after_session=3, fact_id="f02", false_value="dollars", text="x", marker="MARKER-X"
)


def _tool_names(tools: ToolSet | None) -> list[str]:
    assert tools is not None
    return [schema["function"]["name"] for schema in tools.schemas()]


def test_stateless_arm_has_no_tools(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, client=FakeLLM(), env=FakeEnv([question("q1")]), with_memory=False)
    arm = StatelessArm()

    arm.setup(ctx)
    arm.inject_false_note(ctx, NOTE)

    assert arm.name == "A0"
    assert arm.injected_context(ctx, 1) == ""
    assert arm.online_tools(ctx, 1) is None
    assert arm.probe_tools(ctx, 1) is None
    assert arm.after_session(ctx, 1) is None


def test_raw_rag_arm_indexes_transcript_after_session(tmp_path: Path) -> None:
    client = FakeLLM()
    ctx = make_ctx(
        tmp_path, client=client, env=FakeEnv([question("q1")]), arm="A2", with_index=True
    )
    arm = RawRagArm()
    arm.setup(ctx)
    with TranscriptWriter(
        ctx.layout.transcript_file(STREAM_ID, "A2", 1), stream_id=STREAM_ID, session=1
    ) as writer:
        writer.user("Which column holds the customer reference?")
        writer.tool_call("query", {"sql": "SELECT cust_ref FROM orders LIMIT 1"})
        writer.tool_result("query", "cust_ref\n17")

    assert arm.after_session(ctx, 1) is None

    assert [tag.component for tag in client.tags] == ["embedding"]
    tools = arm.online_tools(ctx, 2)
    assert tools is not None
    assert _tool_names(tools) == ["search_transcripts"]
    assert tools is not arm.online_tools(ctx, 2)
    search = ToolCall(id="c", name="search_transcripts", arguments={"query": "cust_ref"})
    reply = tools.dispatch(search)
    assert "[session 1]" in reply and "cust_ref" in reply
    assert client.tags[-1].component == "online"
    assert _tool_names(arm.probe_tools(ctx, 2)) == ["search_transcripts"]
    assert arm.injected_context(ctx, 2) == ""


def test_raw_rag_arm_requires_index(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, client=FakeLLM(), env=FakeEnv([question("q1")]), arm="A2")

    with pytest.raises(ValueError, match="index"):
        RawRagArm().setup(ctx)


def test_oracle_arm_injects_only_valid_facts_in_order(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        client=FakeLLM(),
        env=FakeEnv([question("q1")]),
        arm="A5",
        stream=make_stream(tmp_path / "stream"),
    )
    arm = OracleArm()
    arm.setup(ctx)
    assert ctx.memory is not None

    session_1 = arm.injected_context(ctx, 1)
    session_5 = arm.injected_context(ctx, 5)

    assert "cust_ref" in session_1 and "cancelled" not in session_1
    assert "cust_ref" not in session_5
    assert session_5.index("cents") < session_5.index("cancelled")
    assert ctx.memory.read_all(INJECTED_FILE) == session_5
    assert "cust_ref" not in ctx.memory.read_all(FACTS_FILE)


def test_oracle_arm_moves_overflow_to_facts_file(tmp_path: Path) -> None:
    facts = [
        Fact(
            fact_id=f"f{i:02d}",
            kind="status_code",
            subject=f"order_status code {i}",
            value=f"meaning number {i}",
            valid_from_session=1,
            valid_until_session=None,
            leaf=True,
        )
        for i in range(30)
    ]
    limit = 80
    ctx = make_ctx(
        tmp_path,
        client=FakeLLM(),
        env=FakeEnv([question("q1")]),
        arm="A5",
        stream=make_stream(tmp_path / "stream", facts),
        config=make_config(injected_tokens=limit),
    )
    assert ctx.memory is not None

    injected = OracleArm().injected_context(ctx, 1)

    assert count_tokens(injected) <= limit
    assert FACTS_FILE in injected
    combined = injected + ctx.memory.read_all(FACTS_FILE)
    for i in range(30):
        assert combined.count(f"meaning number {i}\n") == 1


def test_oracle_arm_tools_are_read_only_and_notes_are_ignored(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        client=FakeLLM(),
        env=FakeEnv([question("q1")]),
        arm="A5",
        stream=make_stream(tmp_path / "stream"),
    )
    arm = OracleArm()
    arm.injected_context(ctx, 1)
    assert ctx.memory is not None
    before = ctx.memory.glob("**/*")

    arm.inject_false_note(ctx, NOTE)

    assert ctx.memory.glob("**/*") == before
    assert _tool_names(arm.online_tools(ctx, 1)) == ["memory_read", "memory_search"]
    assert _tool_names(arm.probe_tools(ctx, 1)) == ["memory_read", "memory_search"]
    assert arm.after_session(ctx, 1) is None


def test_oracle_arm_requires_stream_and_memory(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, client=FakeLLM(), env=FakeEnv([question("q1")]), arm="A5")

    with pytest.raises(ValueError, match="stream"):
        OracleArm().setup(ctx)
