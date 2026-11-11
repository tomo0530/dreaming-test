from typing import Any

import pytest
from fakes import FakeLLM, make_tag, text_reply, tool_reply
from project_module.contracts import ChatResult, ToolCall, Usage
from project_module.llm import INVALID_ARGUMENTS_KEY, ContentFiltered
from project_module.llm.ledger import BudgetExceeded
from project_module.llm.loop import LENGTH_NUDGE, LoopResult, run_forced_turn, run_tool_loop

SUBMIT = {
    "type": "function",
    "function": {
        "name": "submit",
        "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}},
    },
}


class RecordingTools:
    """ToolSet that answers `<name>:<arguments>` and fails for the tool named `broken`."""

    def __init__(self) -> None:
        self.dispatched: list[ToolCall] = []

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": name, "parameters": {}}}
            for name in ("a", "b")
        ]

    def dispatch(self, call: ToolCall) -> str:
        self.dispatched.append(call)
        if call.name == "broken":
            return "ERROR: broken tool"
        return f"{call.name}:{call.arguments}"


def _run(
    llm: FakeLLM,
    tools: RecordingTools,
    *,
    max_turns: int = 5,
    max_input_tokens_per_call: int = 10_000,
    max_total_input_tokens: int | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    terminal_tools: list[dict[str, Any]] | None = None,
    force_terminal_on_last_turn: bool = False,
) -> LoopResult:
    return run_tool_loop(
        llm,
        model="luna",
        reasoning_effort="low",
        system="You are a test agent.",
        user="Do the task.",
        tools=tools,
        terminal_tools=[SUBMIT] if terminal_tools is None else terminal_tools,
        max_turns=max_turns,
        max_completion_tokens=128,
        max_input_tokens_per_call=max_input_tokens_per_call,
        max_total_input_tokens=max_total_input_tokens,
        tag=make_tag(),
        on_event=None if events is None else lambda kind, data: events.append((kind, data)),
        force_terminal_on_last_turn=force_terminal_on_last_turn,
    )


def test_loop_stops_on_terminal_tool() -> None:
    llm = FakeLLM([tool_reply(("a", {}), ("submit", {"answer": "42"}), ("b", {}))])
    tools = RecordingTools()

    result = _run(llm, tools)

    assert result.stopped == "terminal"
    assert result.terminal_call is not None
    assert result.terminal_call.arguments == {"answer": "42"}
    assert [c.name for c in tools.dispatched] == ["a"]
    assert result.turns == 1


def test_loop_offers_toolset_and_terminal_schemas() -> None:
    llm = FakeLLM([text_reply("done")])

    _run(llm, RecordingTools())

    sent = llm.calls[0]
    assert [t["function"]["name"] for t in sent["tools"]] == ["a", "b", "submit"]
    assert sent["messages"] == [
        {"role": "system", "content": "You are a test agent."},
        {"role": "user", "content": "Do the task."},
    ]
    assert (sent["model"], sent["reasoning_effort"], sent["max_completion_tokens"]) == (
        "luna",
        "low",
        128,
    )


def test_loop_processes_multiple_tool_calls_in_order() -> None:
    llm = FakeLLM([tool_reply(("b", {"x": 1}), ("a", {"y": 2})), text_reply("done")])
    tools = RecordingTools()

    result = _run(llm, tools)

    assert [c.name for c in tools.dispatched] == ["b", "a"]
    second_turn = llm.calls[1]["messages"]
    assert second_turn[-3]["role"] == "assistant"
    assert second_turn[-2:] == [
        {"role": "tool", "tool_call_id": "call_0", "content": "b:{'x': 1}"},
        {"role": "tool", "tool_call_id": "call_1", "content": "a:{'y': 2}"},
    ]
    assert (result.stopped, result.final_text, result.turns) == ("final", "done", 2)


def test_loop_stops_at_max_turns() -> None:
    llm = FakeLLM([tool_reply(("a", {})) for _ in range(3)])

    result = _run(llm, RecordingTools(), max_turns=2)

    assert (result.stopped, result.turns, len(llm.calls)) == ("max_turns", 2, 2)


@pytest.mark.parametrize(
    ("per_call", "total", "expected_stopped", "expected_turns"),
    [(5, None, "call_input_budget", 0), (10_000, 1_010, "total_input_budget", 1)],
    ids=["per_call", "total"],
)
def test_loop_refuses_call_over_per_call_and_total_budget(
    per_call: int, total: int | None, expected_stopped: str, expected_turns: int
) -> None:
    # The first reply reports 1_000 billed input tokens; the next prompt is well over 10 tokens.
    llm = FakeLLM([tool_reply(("a", {}), input_tokens=1_000), text_reply("never sent")])

    result = _run(
        llm, RecordingTools(), max_input_tokens_per_call=per_call, max_total_input_tokens=total
    )

    assert (result.stopped, result.turns, len(llm.calls)) == (
        expected_stopped,
        expected_turns,
        expected_turns,
    )


def test_loop_returns_invalid_json_arguments_as_error_text() -> None:
    bad = ChatResult(
        content=None,
        tool_calls=[ToolCall(id="c1", name="a", arguments={"__invalid__": "{oops"})],
        usage=Usage(input_tokens=10, output_tokens=5),
        assistant_message={"role": "assistant", "content": None, "tool_calls": []},
    )
    llm = FakeLLM([bad, text_reply("done")])
    tools = RecordingTools()
    events: list[tuple[str, dict[str, Any]]] = []

    _run(llm, tools, events=events)

    tool_message = llm.calls[1]["messages"][-1]
    assert tool_message["tool_call_id"] == "c1"
    assert tool_message["content"].startswith("ERROR: arguments are not valid JSON")
    assert "{oops" in tool_message["content"]
    assert tools.dispatched == []
    assert ("tool_result", {"name": "a", "text": tool_message["content"], "error": True}) in events


def test_loop_does_not_terminate_on_invalid_terminal_arguments() -> None:
    bad_submit = ChatResult(
        content=None,
        tool_calls=[ToolCall(id="c1", name="submit", arguments={"__invalid__": "{"})],
        usage=Usage(input_tokens=10, output_tokens=5),
        assistant_message={"role": "assistant", "content": None, "tool_calls": []},
    )
    llm = FakeLLM([bad_submit, tool_reply(("submit", {"answer": "7"}))])

    result = _run(llm, RecordingTools())

    assert (result.stopped, result.turns) == ("terminal", 2)
    assert llm.calls[1]["messages"][-1]["content"].startswith("ERROR:")


def test_loop_echoes_at_most_200_characters_of_invalid_arguments() -> None:
    raw = "{" + "x" * 5_000
    bad = ChatResult(
        content=None,
        tool_calls=[ToolCall(id="c1", name="a", arguments={"__invalid__": raw})],
        usage=Usage(input_tokens=10, output_tokens=5),
        assistant_message={"role": "assistant", "content": None, "tool_calls": []},
    )
    llm = FakeLLM([bad, text_reply("done")])

    _run(llm, RecordingTools())

    content = llm.calls[1]["messages"][-1]["content"]
    assert content.endswith(raw[:200])
    assert raw[:201] not in content


def test_loop_nudges_after_an_empty_length_cutoff() -> None:
    cut = ChatResult(
        content=None,
        usage=Usage(input_tokens=10, output_tokens=128),
        finish_reason="length",
        assistant_message={"role": "assistant", "content": None},
    )
    llm = FakeLLM([cut, text_reply("complete")])

    result = _run(llm, RecordingTools())

    assert (result.stopped, result.turns) == ("final", 2)
    assert llm.calls[1]["messages"][-1] == {"role": "user", "content": LENGTH_NUDGE}
    assert len(llm.calls[1]["messages"]) == 3


def test_loop_forces_the_terminal_tool_only_on_the_last_turn() -> None:
    llm = FakeLLM([tool_reply(("a", {})), tool_reply(("a", {})), tool_reply(("submit", {}))])

    result = _run(llm, RecordingTools(), max_turns=3, force_terminal_on_last_turn=True)

    assert result.stopped == "terminal"
    assert [call["tool_choice"] for call in llm.calls] == [
        None,
        None,
        {"type": "function", "name": "submit"},
    ]


def test_loop_forces_nothing_by_default() -> None:
    llm = FakeLLM([tool_reply(("a", {})), tool_reply(("a", {}))])

    _run(llm, RecordingTools(), max_turns=2)

    assert [call["tool_choice"] for call in llm.calls] == [None, None]


def test_loop_forces_nothing_without_terminal_tools() -> None:
    llm = FakeLLM([tool_reply(("a", {}))])

    _run(llm, RecordingTools(), max_turns=1, terminal_tools=[], force_terminal_on_last_turn=True)

    assert llm.calls[0]["tool_choice"] is None


def test_loop_refuses_to_force_one_of_several_terminal_tools() -> None:
    other = {"type": "function", "function": {"name": "give_up", "parameters": {}}}
    with pytest.raises(ValueError, match="exactly one terminal tool"):
        _run(
            FakeLLM(),
            RecordingTools(),
            terminal_tools=[SUBMIT, other],
            force_terminal_on_last_turn=True,
        )


def test_loop_continues_after_length_cutoff() -> None:
    cut = text_reply("partial")
    cut.finish_reason = "length"
    llm = FakeLLM([cut, text_reply("complete")])

    result = _run(llm, RecordingTools())

    assert (result.stopped, result.final_text, result.turns) == ("final", "complete", 2)
    assert llm.calls[1]["messages"][-1] == {"role": "assistant", "content": "partial"}


def test_loop_sums_billed_tokens() -> None:
    llm = FakeLLM(
        [
            tool_reply(("a", {}), input_tokens=100, output_tokens=7),
            text_reply("done", input_tokens=150, output_tokens=9),
        ]
    )

    result = _run(llm, RecordingTools())

    assert (result.input_tokens, result.output_tokens) == (250, 16)


def test_loop_reports_events_in_order() -> None:
    reply = tool_reply(("a", {"k": 1}), ("broken", {}))
    reply.content = "thinking aloud"
    llm = FakeLLM([reply, tool_reply(("submit", {"answer": "x"}))])
    events: list[tuple[str, dict[str, Any]]] = []

    _run(llm, RecordingTools(), events=events)

    assert events == [
        ("agent", {"text": "thinking aloud"}),
        ("tool_call", {"name": "a", "arguments": {"k": 1}}),
        ("tool_result", {"name": "a", "text": "a:{'k': 1}", "error": False}),
        ("tool_call", {"name": "broken", "arguments": {}}),
        ("tool_result", {"name": "broken", "text": "ERROR: broken tool", "error": True}),
        ("tool_call", {"name": "submit", "arguments": {"answer": "x"}}),
    ]


def test_loop_rejects_non_positive_max_turns() -> None:
    with pytest.raises(ValueError, match="max_turns must be at least 1"):
        _run(FakeLLM(), RecordingTools(), max_turns=0)


def test_loop_stops_gracefully_on_content_filter() -> None:
    from fakes import FakeLLM, make_tag
    from project_module.llm import ContentFiltered

    class _Filtered(FakeLLM):
        def chat(self, **kwargs: Any) -> Any:  # type: ignore[override]
            raise ContentFiltered("blocked")

    result = run_tool_loop(
        _Filtered(),
        model="m",
        reasoning_effort="low",
        system="s",
        user="u",
        tools=RecordingTools(),
        max_turns=3,
        max_completion_tokens=10,
        max_input_tokens_per_call=10_000,
        max_total_input_tokens=None,
        tag=make_tag(),
    )
    assert (result.stopped, result.turns) == ("content_filter", 0)


def _forced(
    llm: FakeLLM,
    tools: RecordingTools,
    history: list[dict[str, Any]],
    *,
    forced_tool: str = "b",
    max_input_tokens_per_call: int = 10_000,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> str | None:
    return run_forced_turn(
        llm,
        model="luna",
        reasoning_effort="low",
        system="You are a test agent.",
        history=history,
        user="Write the note.",
        tools=tools,
        terminal_tools=[SUBMIT],
        forced_tool=forced_tool,
        max_completion_tokens=64,
        max_input_tokens_per_call=max_input_tokens_per_call,
        tag=make_tag(),
        on_event=None if events is None else lambda kind, data: events.append((kind, data)),
    )


def _assistant_calls(*calls: tuple[str, str]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for call_id, name in calls
        ],
    }


def test_loop_result_keeps_the_conversation_without_system() -> None:
    llm = FakeLLM([tool_reply(("a", {})), tool_reply(("submit", {"answer": "42"}))])

    result = _run(llm, RecordingTools())

    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert result.messages[0] == {"role": "user", "content": "Do the task."}
    assert result.messages[-1]["tool_calls"][0]["function"]["name"] == "submit"


def test_run_forced_turn_dispatches_once() -> None:
    llm = FakeLLM([tool_reply(("b", {"note": "x"}), ("a", {}))])
    tools = RecordingTools()
    history = [{"role": "user", "content": "Do the task."}, {"role": "assistant", "content": "ok"}]
    events: list[tuple[str, dict[str, Any]]] = []

    text = _forced(llm, tools, history, events=events)

    assert text == "b:{'note': 'x'}"
    assert [c.name for c in tools.dispatched] == ["b"]
    assert len(llm.calls) == 1
    sent = llm.calls[0]
    assert sent["tool_choice"] == {"type": "function", "name": "b"}
    assert [t["function"]["name"] for t in sent["tools"]] == ["a", "b", "submit"]
    assert sent["messages"] == [
        {"role": "system", "content": "You are a test agent."},
        *history,
        {"role": "user", "content": "Write the note."},
    ]
    assert sent["max_completion_tokens"] == 64
    assert events == [
        ("tool_call", {"name": "b", "arguments": {"note": "x"}}),
        ("tool_result", {"name": "b", "text": "b:{'note': 'x'}", "error": False}),
    ]


def test_run_forced_turn_closes_every_unanswered_call_of_the_last_reply() -> None:
    llm = FakeLLM([tool_reply(("b", {}))])
    history = [
        {"role": "user", "content": "Do the task."},
        _assistant_calls(("c1", "a"), ("c2", "submit"), ("c3", "b")),
        {"role": "tool", "tool_call_id": "c1", "content": "a:{}"},
    ]

    _forced(llm, RecordingTools(), history)

    sent = llm.calls[0]["messages"]
    assert sent[len(history) + 1 : -1] == [
        {"role": "tool", "tool_call_id": "c2", "content": "Submitted."},
        {"role": "tool", "tool_call_id": "c3", "content": "Not executed: the question is closed."},
    ]
    assert sent[-1] == {"role": "user", "content": "Write the note."}


def test_run_forced_turn_skips_the_call_over_the_input_budget() -> None:
    llm = FakeLLM([tool_reply(("b", {}))])
    tools = RecordingTools()

    text = _forced(llm, tools, [{"role": "user", "content": "x"}], max_input_tokens_per_call=5)

    assert text is None
    assert llm.calls == []
    assert tools.dispatched == []


def test_run_forced_turn_returns_none_on_content_filter() -> None:
    class _Filtered(FakeLLM):
        def chat(self, **kwargs: Any) -> Any:  # type: ignore[override]
            raise ContentFiltered("blocked")

    tools = RecordingTools()

    assert _forced(_Filtered(), tools, [{"role": "user", "content": "x"}]) is None
    assert tools.dispatched == []


def test_run_forced_turn_propagates_budget_exceeded() -> None:
    class _Broke(FakeLLM):
        def chat(self, **kwargs: Any) -> Any:  # type: ignore[override]
            raise BudgetExceeded("phase cap reached")

    with pytest.raises(BudgetExceeded):
        _forced(_Broke(), RecordingTools(), [{"role": "user", "content": "x"}])


def test_run_forced_turn_returns_invalid_arguments_as_error_without_dispatch() -> None:
    llm = FakeLLM([tool_reply(("b", {INVALID_ARGUMENTS_KEY: "not json"}))])
    tools = RecordingTools()

    text = _forced(llm, tools, [{"role": "user", "content": "x"}])

    assert text is not None and text.startswith("ERROR: arguments are not valid JSON")
    assert tools.dispatched == []


def test_run_forced_turn_rejects_a_tool_outside_the_tool_set() -> None:
    with pytest.raises(ValueError, match="forced tool 'submit' is not in the tool set"):
        _forced(FakeLLM(), RecordingTools(), [], forced_tool="submit")
