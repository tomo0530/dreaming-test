import logging
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeLLM, text_reply, tool_reply
from project_module.agent.context import ArmContext, StopRequested
from project_module.agent.online_arms import RawRagArm
from project_module.agent.session import CombinedToolSet, question_query_tool, run_session
from project_module.config import ExperimentConfig
from project_module.contracts import ChatResult, CostTag, ToolCall, ToolSet
from project_module.llm import ContentFiltered
from project_module.llm.ledger import BudgetExceeded
from project_module.memory.tools import MemoryReadTools
from project_module.prompts import load_prompt
from project_module.tokens import count_tokens

from .fake_env import (
    GOLD_SQL,
    MARKER,
    STREAM_ID,
    FakeEnv,
    ScriptedArm,
    make_config,
    make_ctx,
    make_stream,
    question,
)


def _submit(sql: str = GOLD_SQL) -> ChatResult:
    return tool_reply(("submit_sql", {"sql": sql}))


def _query(sql: str) -> ChatResult:
    return tool_reply(("query", {"sql": sql}))


def _memory_tools(ctx: ArmContext) -> ToolSet:
    assert ctx.memory is not None
    return MemoryReadTools(ctx.memory, token_budget=500, writable_prefix=None)


def _transcript(ctx: ArmContext, arm: str, session: int) -> str:
    return ctx.layout.transcript_file(STREAM_ID, arm, session).read_text(encoding="utf-8")


def test_session_records_no_submission(tmp_path: Path) -> None:
    env = FakeEnv([question("q1"), question("q2")])
    client = FakeLLM([text_reply("I cannot tell."), _submit()])
    ctx = make_ctx(tmp_path, client=client, env=env)

    results = run_session(ScriptedArm(), ctx, 1)

    assert [r.category for r in results] == ["no_submission", "correct"]
    assert [r.qid for r in results] == ["q1", "q2"]
    assert env.graded[0] == ("q1", None, 0)
    assert results[0].turns == 1


def test_session_records_query_counts_and_active_markers(tmp_path: Path) -> None:
    env = FakeEnv([question("q1")])
    client = FakeLLM(
        [
            _query("SELECT count(*) FROM orders"),
            _query("SELECT bad FROM orders"),
            tool_reply(("memory_read", {"path": "notes/missing.md"})),
            _submit(),
        ]
    )
    ctx = make_ctx(tmp_path, client=client, env=env, stream=make_stream(tmp_path / "stream"))
    assert ctx.memory is not None
    ctx.memory.write("notes/amount.md", f"Amounts are in dollars.\n{MARKER}\n")

    [result] = run_session(ScriptedArm(tools=_memory_tools), ctx, 1)

    assert (result.queries, result.ok_queries) == (2, 1)
    assert (result.tool_calls, result.tool_errors) == (3, 2)
    assert result.turns == 4
    assert result.active_markers == [MARKER]
    assert result.category == "correct"
    assert env.graded == [("q1", GOLD_SQL, 2)]


def test_active_markers_are_empty_without_memory(tmp_path: Path) -> None:
    env = FakeEnv([question("q1")])
    ctx = make_ctx(
        tmp_path,
        client=FakeLLM([_submit()]),
        env=env,
        stream=make_stream(tmp_path / "stream"),
        with_memory=False,
    )

    [result] = run_session(ScriptedArm("A0"), ctx, 1)

    assert result.active_markers == []


def test_session_enforces_env_max_queries(tmp_path: Path) -> None:
    env = FakeEnv([question("q1")], max_queries=3)
    script = [_query(f"SELECT {i}") for i in range(4)] + [_submit()]
    client = FakeLLM(script)
    ctx = make_ctx(tmp_path, client=client, env=env)

    [result] = run_session(ScriptedArm(), ctx, 1)

    assert [sql for _, sql in env.executed] == ["SELECT 0", "SELECT 1", "SELECT 2"]
    assert (result.queries, result.ok_queries, result.tool_errors) == (4, 3, 1)
    assert env.graded == [("q1", GOLD_SQL, 4)]
    last_tool_message = client.calls[-1]["messages"][-1]
    assert last_tool_message["role"] == "tool"
    assert last_tool_message["content"].startswith("ERROR: query limit reached")


def test_query_tool_rejects_missing_sql_without_counting() -> None:
    env = FakeEnv([question("q1")])
    tool = question_query_tool(env, 1)

    reply = tool.dispatch(ToolCall(id="c", name="query", arguments={}))

    assert reply.startswith("ERROR:")
    assert tool.attempts == 0
    assert env.executed == []


def test_session_transcript_has_no_grading_or_memory_content(tmp_path: Path) -> None:
    env = FakeEnv([question("q1", text="Total revenue of shipped orders?")])
    client = FakeLLM(
        [
            tool_reply(("memory_read", {"path": "notes/a.md"})),
            _query("SELECT 2"),
            _submit("SELECT 2"),
        ]
    )
    ctx = make_ctx(tmp_path, client=client, env=env)
    assert ctx.memory is not None
    ctx.memory.write("notes/a.md", "MEMORY-BODY-TEXT: revenue lives in orders.total\n")
    arm = ScriptedArm(injected="INJECTED-LAYER-TEXT", tools=_memory_tools)

    [result] = run_session(arm, ctx, 1)

    text = _transcript(ctx, "A1", 1)
    assert result.category == "wrong_result"
    assert "USER: Total revenue of shipped orders?" in text
    assert 'TOOL CALL submit_sql: {"sql": "SELECT 2"}' in text
    assert "[retrieved content omitted]" in text
    for leaked in (
        "MEMORY-BODY-TEXT",
        "INJECTED-LAYER-TEXT",
        load_prompt("agent_system.md").splitlines()[0],
        GOLD_SQL,
        "wrong_result",
        "correct",
    ):
        assert leaked not in text
    # The model itself does see the injected layer.
    assert "INJECTED-LAYER-TEXT" in client.calls[0]["messages"][0]["content"]


def test_session_writes_feedback_event_only_when_env_returns_it(tmp_path: Path) -> None:
    feedback = "Question 1: INCORRECT.\nCorrect answer: 42"
    with_feedback = FakeEnv([question("q1"), question("q2")], feedback_text=feedback)
    client = FakeLLM([_submit("SELECT 2"), _submit()])
    ctx = make_ctx(tmp_path / "a", client=client, env=with_feedback)

    run_session(ScriptedArm(), ctx, 1)

    assert "EVENT feedback: Question 1: INCORRECT.\n Correct answer: 42" in _transcript(
        ctx, "A1", 1
    )
    second_question_messages = str(client.calls[1]["messages"])
    assert "Correct answer: 42" not in second_question_messages

    silent = FakeEnv([question("q1")])
    silent_ctx = make_ctx(tmp_path / "b", client=FakeLLM([_submit()]), env=silent)
    run_session(ScriptedArm(), silent_ctx, 1)
    assert "EVENT" not in _transcript(silent_ctx, "A1", 1)


@pytest.mark.parametrize("arm_name", ["A1", "A2", "A3", "A4", "A5"])
def test_injected_context_is_truncated_for_every_arm(tmp_path: Path, arm_name: Any) -> None:
    limit = 40
    env = FakeEnv([question("q1")])
    client = FakeLLM([_submit()])
    config = make_config(injected_tokens=limit)
    ctx = make_ctx(tmp_path, client=client, env=env, arm=arm_name, config=config)
    long_memory = " ".join(f"rule-{i} holds." for i in range(500))

    run_session(ScriptedArm(arm_name, injected=long_memory), ctx, 1)

    system = client.calls[0]["messages"][0]["content"]
    memory = system.split("<memory>\n", 1)[1].split("\n</memory>", 1)[0]
    assert memory.endswith("[truncated]")
    assert count_tokens(memory) <= limit


def test_session_without_injected_context_has_no_memory_block(tmp_path: Path) -> None:
    client = FakeLLM([_submit()])
    ctx = make_ctx(tmp_path, client=client, env=FakeEnv([question("q1")]))

    run_session(ScriptedArm(), ctx, 1)

    assert client.calls[0]["messages"][0]["content"] == load_prompt("agent_system.md")


def test_session_uses_fresh_online_tools_and_agent_settings_per_question(tmp_path: Path) -> None:
    env = FakeEnv([question("q1"), question("q2")])
    client = FakeLLM([_submit(), _submit()])
    ctx = make_ctx(tmp_path, client=client, env=env)
    arm = ScriptedArm(tools=_memory_tools)

    run_session(arm, ctx, 1)

    assert arm.tool_requests == 2
    tool_names = [t["function"]["name"] for t in client.calls[0]["tools"]]
    assert tool_names == ["query", "memory_read", "memory_search", "submit_sql"]
    assert {c["model"] for c in client.calls} == {"gpt-6-luna"}
    assert {c["reasoning_effort"] for c in client.calls} == {"low"}
    assert client.tags[0] == CostTag(
        phase="test",
        run_id="run-fake",
        stream_id=STREAM_ID,
        arm="A1",
        session=1,
        component="online",
    )


def test_numeric_submission_is_graded_as_text(tmp_path: Path) -> None:
    env = FakeEnv([question("q1"), question("q2")], submit_param="value")
    client = FakeLLM(
        [tool_reply(("submit_sql", {"value": 42})), tool_reply(("submit_sql", {"value": True}))]
    )
    ctx = make_ctx(tmp_path, client=client, env=env)

    run_session(ScriptedArm(), ctx, 1)

    assert [submission for _, submission, _ in env.graded] == ["42", None]


class _BrokeClient(FakeLLM):
    def chat(self, **kwargs: Any) -> ChatResult:
        raise BudgetExceeded("phase cap reached")


def test_budget_exceeded_propagates(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, client=_BrokeClient(), env=FakeEnv([question("q1")]))

    with pytest.raises(BudgetExceeded):
        run_session(ScriptedArm(), ctx, 1)

    assert _transcript(ctx, "A1", 1).startswith(f"SESSION stream={STREAM_ID} session=001")


def test_combined_toolset_rejects_duplicate_names() -> None:
    env = FakeEnv([question("q1")])

    with pytest.raises(ValueError, match="duplicate tool name"):
        CombinedToolSet([question_query_tool(env, 1), question_query_tool(env, 1)])


def test_combined_toolset_reports_unknown_tool_as_error() -> None:
    tools = CombinedToolSet([])

    assert tools.dispatch(ToolCall(id="c", name="nope", arguments={})).startswith("ERROR:")


def test_session_counts_a_reworded_false_claim_as_an_active_marker(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        client=FakeLLM([_submit()]),
        env=FakeEnv([question("q1")]),
        stream=make_stream(tmp_path / "stream"),
    )
    assert ctx.memory is not None
    ctx.memory.write("notes/amount.md", "- ORDERS.AMOUNT holds dollars.\n")

    [result] = run_session(ScriptedArm(tools=_memory_tools), ctx, 1)

    assert result.active_markers == [MARKER]


def test_session_stops_before_the_next_question_when_asked(tmp_path: Path) -> None:
    env = FakeEnv([question("q1"), question("q2")])
    client = FakeLLM([_submit(), _submit()])
    ctx = make_ctx(tmp_path, client=client, env=env)
    grade = env.grade

    def grade_then_stop(*args: Any) -> Any:
        ctx.stop.request("interrupted")
        return grade(*args)

    env.grade = grade_then_stop  # type: ignore[method-assign]

    with pytest.raises(StopRequested):
        run_session(ScriptedArm(), ctx, 1)

    assert [qid for qid, _, _ in env.graded] == ["q1"]
    assert len(client.calls) == 1


def test_session_forces_the_submit_tool_on_the_last_turn(tmp_path: Path) -> None:
    client = FakeLLM([_query("SELECT 1") for _ in range(5)] + [_submit()])
    ctx = make_ctx(tmp_path, client=client, env=FakeEnv([question("q1")]))

    [result] = run_session(ScriptedArm(), ctx, 1)

    choices = [call["tool_choice"] for call in client.calls]
    assert choices == [None] * 5 + [{"type": "function", "name": "submit_sql"}]
    assert result.category == "correct"


def test_session_info_logs_carry_no_grades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = FakeEnv([question("q1"), question("q2")])
    ctx = make_ctx(tmp_path, client=FakeLLM([_submit(), _submit("SELECT 2")]), env=env)

    with caplog.at_level(logging.INFO, logger="project_module"):
        run_session(ScriptedArm(), ctx, 1)

    info = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "turns" in info and "tool errors" in info
    for grade in ("correct", "wrong_result", "stale", "sql_error", "no_submission"):
        assert grade not in info


def test_a2_transcript_and_index_never_hold_retrieved_passages(tmp_path: Path) -> None:
    retrieved = "PASSAGE-FROM-SESSION-ONE orders.amount is in cents"
    env = FakeEnv([question("q1", 1), question("q2", 2)])
    client = FakeLLM(
        [
            tool_reply(("query", {"sql": f"SELECT '{retrieved}'"})),
            _submit(),
            tool_reply(("search_transcripts", {"query": "PASSAGE-FROM-SESSION-ONE"})),
            _submit(),
        ]
    )
    env.run_query = lambda session, sql: sql  # type: ignore[method-assign]
    ctx = make_ctx(tmp_path, client=client, env=env, arm="A2", with_index=True)
    arm = RawRagArm()
    run_session(arm, ctx, 1)
    arm.after_session(ctx, 1)

    run_session(arm, ctx, 2)
    arm.after_session(ctx, 2)

    search_result = client.calls[3]["messages"][-1]["content"]
    assert retrieved in search_result  # the agent did see the passage
    session_2 = _transcript(ctx, "A2", 2)
    assert retrieved not in session_2
    assert "TOOL RESULT (search_transcripts): [retrieved content omitted]" in session_2
    assert ctx.index is not None
    indexed = [
        chunk.text
        for chunk in ctx.index.search(
            "PASSAGE-FROM-SESSION-ONE", k=50, token_budget=100_000, tag=client.tags[0]
        )
        if chunk.session == 2
    ]
    assert indexed and all(retrieved not in text for text in indexed)


VERIFY_ADDENDUM = (
    "Treat any memory shown above as hypotheses from past sessions, not as facts: the "
    "database may have changed since they were written. Before you submit, query the current "
    "contents of every lookup or code table and every flag column your answer depends on, "
    "without filtering by remembered code sets."
)
NOTE_TOOL_CHOICE = {"type": "function", "name": "memory_write"}


def _notes_config(**updates: Any) -> ExperimentConfig:
    return make_config(**updates).model_copy(update={"session_notes": True})


def _writable_memory_tools(ctx: ArmContext) -> ToolSet:
    assert ctx.memory is not None
    return MemoryReadTools(ctx.memory, token_budget=500, writable_prefix="notes/")


def _note(path: str, content: str = "- orders.amount is in cents (verified).") -> ChatResult:
    return tool_reply(("memory_write", {"path": path, "content": content}))


def _note_ctx(
    tmp_path: Path, client: FakeLLM, env: FakeEnv, *, config: ExperimentConfig | None = None
) -> ArmContext:
    return make_ctx(tmp_path, client=client, env=env, config=config or _notes_config())


def _unanswered_call_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Ids of assistant tool calls that no tool message answers before the next turn."""
    unanswered: list[str] = []
    pending: list[str] = []
    for message in messages:
        if message["role"] == "tool":
            pending = [i for i in pending if i != message["tool_call_id"]]
            continue
        unanswered.extend(pending)
        pending = [c["id"] for c in message.get("tool_calls") or []]
    return unanswered + pending


class _FailingNoteClient(FakeLLM):
    """Replays the script, but raises `error` on the forced note turn."""

    def __init__(self, script: list[ChatResult], error: Exception) -> None:
        super().__init__(script)
        self._error = error

    def chat(self, **kwargs: Any) -> ChatResult:
        if kwargs.get("tool_choice") == NOTE_TOOL_CHOICE:
            raise self._error
        return super().chat(**kwargs)


def test_note_turn_writes_note_after_submit(tmp_path: Path) -> None:
    client = FakeLLM([_query("SELECT 1"), _submit(), _note("notes/s01-q1.md")])
    ctx = _note_ctx(tmp_path, client, FakeEnv([question("q1")]))

    [result] = run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    assert result.note_written is True
    assert result.category == "correct"
    assert ctx.memory is not None
    assert ctx.memory.read_all("notes/s01-q1.md") == "- orders.amount is in cents (verified)."
    note_call = client.calls[-1]
    assert note_call["tool_choice"] == NOTE_TOOL_CHOICE
    assert note_call["max_completion_tokens"] == 1024
    assert "notes/s01-q1.md" in note_call["messages"][-1]["content"]
    assert client.tags[-1].component == "online"


def test_note_turn_closes_all_unanswered_calls(tmp_path: Path) -> None:
    same_reply = tool_reply(("submit_sql", {"sql": GOLD_SQL}), ("query", {"sql": "SELECT 9"}))
    client = FakeLLM([same_reply, _note("notes/s01-q1.md")])
    env = FakeEnv([question("q1")])
    ctx = _note_ctx(tmp_path, client, env)

    run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    sent = client.calls[-1]["messages"]
    assert _unanswered_call_ids(sent) == []
    assert sent[-3:-1] == [
        {"role": "tool", "tool_call_id": "call_0", "content": "Submitted."},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Not executed: the question is closed.",
        },
    ]
    assert env.executed == []


def test_note_turn_runs_after_no_submission(tmp_path: Path) -> None:
    client = FakeLLM([_query("SELECT 1"), _query("SELECT 2"), _note("notes/s01-q1.md")])
    ctx = _note_ctx(tmp_path, client, FakeEnv([question("q1")]), config=_notes_config(max_turns=2))

    [result] = run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    assert result.category == "no_submission"
    assert result.note_written is True
    assert len(client.calls) == 3
    assert _unanswered_call_ids(client.calls[-1]["messages"]) == []


def _read_only_memory_tools(ctx: ArmContext) -> ToolSet:
    return _memory_tools(ctx)


@pytest.mark.parametrize(
    ("arm_name", "tools", "session_notes"),
    [
        ("A0", None, True),
        ("A1", _read_only_memory_tools, True),
        ("A1", _writable_memory_tools, False),
    ],
    ids=["no-tools", "read-only-memory", "notes-switched-off"],
)
def test_note_turn_skipped_without_memory_write_tool(
    tmp_path: Path, arm_name: Any, tools: Any, session_notes: bool
) -> None:
    client = FakeLLM([_submit(), _note("notes/s01-q1.md")])
    config = make_config().model_copy(update={"session_notes": session_notes})
    ctx = make_ctx(
        tmp_path, client=client, env=FakeEnv([question("q1")]), arm=arm_name, config=config
    )

    [result] = run_session(ScriptedArm(arm_name, tools=tools), ctx, 1)

    assert len(client.calls) == 1
    assert result.note_written is False


def test_note_turn_content_filter_is_recorded_not_raised(tmp_path: Path) -> None:
    client = _FailingNoteClient([_submit()], ContentFiltered("blocked"))
    ctx = _note_ctx(tmp_path, client, FakeEnv([question("q1")]))

    [result] = run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    assert result.note_written is False
    assert result.category == "correct"


def test_note_turn_propagates_budget_exceeded(tmp_path: Path) -> None:
    client = _FailingNoteClient([_submit()], BudgetExceeded("phase cap reached"))
    env = FakeEnv([question("q1")])
    ctx = _note_ctx(tmp_path, client, env)

    with pytest.raises(BudgetExceeded):
        run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)


def test_notes_of_one_session_do_not_overwrite(tmp_path: Path) -> None:
    client = FakeLLM(
        [
            _submit(),
            _note("notes/s03-q1.md", "note one"),
            _submit(),
            _note("notes/s03-q2.md", "note two"),
        ]
    )
    env = FakeEnv([question("q1", 3), question("q2", 3)])
    ctx = _note_ctx(tmp_path, client, env)

    results = run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 3)

    assert [r.note_written for r in results] == [True, True]
    assert "notes/s03-q1.md" in client.calls[1]["messages"][-1]["content"]
    assert "notes/s03-q2.md" in client.calls[3]["messages"][-1]["content"]
    assert ctx.memory is not None
    assert ctx.memory.read_all("notes/s03-q1.md") == "note one"
    assert ctx.memory.read_all("notes/s03-q2.md") == "note two"


def test_note_written_is_false_when_the_note_goes_elsewhere(tmp_path: Path) -> None:
    client = FakeLLM([_submit(), _note("notes/other.md")])
    ctx = _note_ctx(tmp_path, client, FakeEnv([question("q1")]))

    [result] = run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    assert result.note_written is False


def test_note_turn_not_counted_in_result_tool_calls(tmp_path: Path) -> None:
    # The note write fails (outside notes/), so it would show up as a tool error if counted.
    client = FakeLLM([_query("SELECT 1"), _submit(), _note("wiki/elsewhere.md")])
    ctx = _note_ctx(tmp_path, client, FakeEnv([question("q1")]))

    [result] = run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    assert len(client.calls) == 3
    assert (result.tool_calls, result.tool_errors, result.turns) == (1, 0, 2)
    assert "TOOL RESULT (memory_write) [error]:" in _transcript(ctx, "A1", 1)


def test_note_turn_sends_same_tool_schemas_as_answer_loop(tmp_path: Path) -> None:
    client = FakeLLM([_query("SELECT 1"), _submit(), _note("notes/s01-q1.md")])
    ctx = _note_ctx(tmp_path, client, FakeEnv([question("q1")]))

    run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    names = [t["function"]["name"] for t in client.calls[-1]["tools"]]
    assert names == ["query", "memory_read", "memory_search", "memory_write", "submit_sql"]
    assert client.calls[-1]["tools"] == client.calls[0]["tools"]
    assert client.calls[-1]["messages"][0] == client.calls[0]["messages"][0]


@pytest.mark.parametrize("arm_name", ["A0", "A1", "A4"])
def test_feedback_never_reaches_agent_messages(tmp_path: Path, arm_name: Any) -> None:
    env = FakeEnv(
        [question("q1"), question("q2")],
        feedback_text="Question 1: INCORRECT.\nCorrect answer: 42",
    )
    if arm_name == "A0":
        script = [_submit("SELECT 2"), _submit()]
        arm = ScriptedArm("A0")
    else:
        script = [
            _submit("SELECT 2"),
            _note("notes/s01-q1.md"),
            _submit(),
            _note("notes/s01-q2.md"),
        ]
        arm = ScriptedArm(arm_name, tools=_writable_memory_tools)
    client = FakeLLM(script)
    ctx = _note_ctx(tmp_path, client, env)

    run_session(arm, ctx, 1)

    assert "EVENT feedback:" in _transcript(ctx, arm_name, 1)
    assert len(client.calls) == len(script)
    for call in client.calls:
        sent = str(call["messages"])
        assert "CORRECT" not in sent
        assert "Correct answer" not in sent


def test_feedback_line_written_after_note(tmp_path: Path) -> None:
    env = FakeEnv([question("q1")], feedback_text="Question 1: CORRECT!")
    client = FakeLLM([_submit(), _note("notes/s01-q1.md")])
    ctx = _note_ctx(tmp_path, client, env)

    run_session(ScriptedArm(tools=_writable_memory_tools), ctx, 1)

    text = _transcript(ctx, "A1", 1)
    assert "TOOL CALL memory_write:" in text
    assert text.index("TOOL CALL memory_write:") < text.index("EVENT feedback:")


def _verify_config() -> ExperimentConfig:
    return make_config().model_copy(update={"agent_prompt_addendum": "verify_memory"})


def test_verify_addendum_text_is_the_fixed_design_text() -> None:
    assert load_prompt("agent_verify_addendum.md") == VERIFY_ADDENDUM


def test_verify_addendum_follows_memory_block(tmp_path: Path) -> None:
    client = FakeLLM([_submit()])
    ctx = make_ctx(tmp_path, client=client, env=FakeEnv([question("q1")]), config=_verify_config())

    run_session(ScriptedArm(injected="REMEMBERED-RULE"), ctx, 1)

    system = client.calls[0]["messages"][0]["content"]
    assert system.endswith("</memory>\n\n" + VERIFY_ADDENDUM)
    assert system.index("REMEMBERED-RULE") < system.index(VERIFY_ADDENDUM)


def test_verify_addendum_follows_the_base_prompt_without_memory(tmp_path: Path) -> None:
    client = FakeLLM([_submit()])
    ctx = make_ctx(
        tmp_path,
        client=client,
        env=FakeEnv([question("q1")]),
        arm="A0",
        with_memory=False,
        config=_verify_config(),
    )

    run_session(ScriptedArm("A0"), ctx, 1)

    expected = load_prompt("agent_system.md") + "\n\n" + VERIFY_ADDENDUM
    assert client.calls[0]["messages"][0]["content"] == expected


def test_verify_addendum_is_absent_by_default(tmp_path: Path) -> None:
    client = FakeLLM([_submit()])
    ctx = make_ctx(tmp_path, client=client, env=FakeEnv([question("q1")]))

    run_session(ScriptedArm(injected="REMEMBERED-RULE"), ctx, 1)

    assert VERIFY_ADDENDUM not in client.calls[0]["messages"][0]["content"]


def _auto_rag_session_2(tmp_path: Path, text: str) -> tuple[ArmContext, FakeLLM, list[Any]]:
    """Run A2 with automatic retrieval for session 1 (indexed) and session 2."""
    env = FakeEnv([question("q1", 1), question("q2", 2, text=text)])
    client = FakeLLM([_query("SELECT cust_ref FROM orders LIMIT 1"), _submit(), _submit()])
    config = make_config().model_copy(update={"a2_retrieval": "auto"})
    ctx = make_ctx(tmp_path, client=client, env=env, arm="A2", with_index=True, config=config)
    arm = RawRagArm()
    first = run_session(arm, ctx, 1)
    arm.after_session(ctx, 1)
    second = run_session(arm, ctx, 2)
    return ctx, client, [*first, *second]


def test_auto_retrieval_prepends_excerpts(tmp_path: Path) -> None:
    text = "Which column holds the cust_ref of an order?"

    _, client, results = _auto_rag_session_2(tmp_path, text)

    user = client.calls[-1]["messages"][1]["content"]
    assert user.startswith("Relevant excerpts from past sessions:\n")
    assert "SELECT cust_ref FROM orders LIMIT 1" in user
    assert user.endswith("\n\n" + text)
    assert [r.retrieved_tokens > 0 for r in results] == [False, True]
    assert client.calls[0]["messages"][1]["content"] == "How many orders are there? (q1)"


def test_auto_retrieval_not_written_to_transcript(tmp_path: Path) -> None:
    text = "Which column holds the cust_ref of an order?"

    ctx, _, _ = _auto_rag_session_2(tmp_path, text)

    transcript = _transcript(ctx, "A2", 2)
    assert f"USER: {text}\n" in transcript
    assert "Relevant excerpts" not in transcript
    assert "TOOL CALL query" not in transcript
