from typing import Any

import pytest
from fakes import FakeLLM, make_tag, text_reply, tool_reply
from project_module.contracts import ChatResult, ToolCall, Usage
from project_module.llm.loop import LoopResult, run_tool_loop

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
) -> LoopResult:
    return run_tool_loop(
        llm,
        model="luna",
        reasoning_effort="low",
        system="You are a test agent.",
        user="Do the task.",
        tools=tools,
        terminal_tools=[SUBMIT],
        max_turns=max_turns,
        max_completion_tokens=128,
        max_input_tokens_per_call=max_input_tokens_per_call,
        max_total_input_tokens=max_total_input_tokens,
        tag=make_tag(),
        on_event=None if events is None else lambda kind, data: events.append((kind, data)),
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
