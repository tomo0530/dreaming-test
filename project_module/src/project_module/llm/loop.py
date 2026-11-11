"""Bounded tool-calling loop shared by the online agent, probes and consolidation phases."""

import logging
from collections.abc import Callable, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel

from project_module.contracts import CostTag, LLMClient, ToolCall, ToolChoice, ToolSet
from project_module.llm import INVALID_ARGUMENTS_KEY, ContentFiltered
from project_module.tokens import count_chat_tokens

logger = logging.getLogger(__name__)

ERROR_PREFIX: Final = "ERROR:"
# How much of unparseable tool arguments is echoed back; the rest only costs input tokens.
RAW_ARGUMENTS_EXCERPT_CHARS: Final = 200
LENGTH_NUDGE: Final = "Your reply was cut off by the output limit. Make smaller, targeted edits."
# Outputs given to calls left unanswered when a loop ended, before a forced turn continues it.
SUBMITTED_OUTPUT: Final = "Submitted."
CLOSED_OUTPUT: Final = "Not executed: the question is closed."

LoopStop = Literal[
    "terminal", "final", "max_turns", "call_input_budget", "total_input_budget", "content_filter"
]
EventHandler = Callable[[str, dict[str, Any]], None]


class LoopResult(BaseModel):
    final_text: str | None
    terminal_call: ToolCall | None
    turns: int
    stopped: LoopStop
    input_tokens: int
    output_tokens: int
    # The conversation this loop built, without the system message.
    messages: list[dict[str, Any]]


def _stop(result: LoopResult, stopped: LoopStop, messages: list[dict[str, Any]]) -> LoopResult:
    result.stopped = stopped
    result.messages = messages[1:]
    logger.debug("Tool loop stopped (%s) after %d turns", stopped, result.turns)
    return result


def _tool_name(schema: dict[str, Any]) -> str:
    return str(schema["function"]["name"])


def _emit(on_event: EventHandler | None, kind: str, data: dict[str, Any]) -> None:
    if on_event is not None:
        on_event(kind, data)


def _invalid_arguments_error(call: ToolCall) -> str | None:
    raw = call.arguments.get(INVALID_ARGUMENTS_KEY)
    if raw is None:
        return None
    excerpt = str(raw)[:RAW_ARGUMENTS_EXCERPT_CHARS]
    return f"{ERROR_PREFIX} arguments are not valid JSON objects: {excerpt}"


def _forced_choice(terminal_tools: Sequence[dict[str, Any]]) -> ToolChoice:
    if len(terminal_tools) != 1:
        raise ValueError(
            "force_terminal_on_last_turn needs exactly one terminal tool, "
            f"got {len(terminal_tools)}"
        )
    return {"type": "function", "name": _tool_name(terminal_tools[0])}


def run_tool_loop(
    client: LLMClient,
    *,
    model: str,
    reasoning_effort: str,
    system: str,
    user: str,
    tools: ToolSet,
    terminal_tools: Sequence[dict[str, Any]] = (),
    max_turns: int,
    max_completion_tokens: int,
    max_input_tokens_per_call: int,
    max_total_input_tokens: int | None,
    tag: CostTag,
    on_event: EventHandler | None = None,
    force_terminal_on_last_turn: bool = False,
) -> LoopResult:
    """Run one conversation until a terminal tool, a plain answer, or a budget stops it.

    A turn is one model call. Before each call the prompt is counted locally; the loop stops
    without calling when that count exceeds `max_input_tokens_per_call`, or when billed input
    so far plus that count exceeds `max_total_input_tokens`. The tool calls of one reply are
    handled in order; a call to a terminal tool ends the loop and is returned unexecuted.
    Calls whose arguments are not a JSON object get an `ERROR:` text (quoting at most
    `RAW_ARGUMENTS_EXCERPT_CHARS` characters of them) instead of dispatch, and a terminal call
    in that state does not end the loop. A reply cut off by `finish_reason == "length"`
    without tool calls uses up its turn and the loop goes on; when it has no text either, the
    user turn `LENGTH_NUDGE` is added first so the next call does not repeat the same prompt.
    With `force_terminal_on_last_turn` and a terminal tool, the last allowed call is sent
    with a tool choice that forces that tool.

    Events passed to `on_event`: `("agent", {"text"})` for non-empty reply text,
    `("tool_call", {"name", "arguments"})` for every call including the terminal one, and
    `("tool_result", {"name", "text", "error"})` for every non-terminal call.

    Raises
    ------
    ValueError
        If `max_turns` is less than 1, or `force_terminal_on_last_turn` is set while there
        are several terminal tools.
    """
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    schemas = [*tools.schemas(), *terminal_tools]
    terminal_names = {_tool_name(schema) for schema in terminal_tools}
    last_turn_choice = (
        _forced_choice(terminal_tools) if force_terminal_on_last_turn and terminal_tools else None
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    result = LoopResult(
        final_text=None,
        terminal_call=None,
        turns=0,
        stopped="max_turns",
        input_tokens=0,
        output_tokens=0,
        messages=[],
    )
    while result.turns < max_turns:
        prompt_tokens = count_chat_tokens(messages, schemas or None)
        if prompt_tokens > max_input_tokens_per_call:
            return _stop(result, "call_input_budget", messages)
        if (
            max_total_input_tokens is not None
            and result.input_tokens + prompt_tokens > max_total_input_tokens
        ):
            return _stop(result, "total_input_budget", messages)

        last_turn = result.turns == max_turns - 1
        try:
            reply = client.chat(
                model=model,
                messages=messages,
                tools=schemas or None,
                reasoning_effort=reasoning_effort,
                max_completion_tokens=max_completion_tokens,
                tag=tag,
                tool_choice=last_turn_choice if last_turn else None,
            )
        except ContentFiltered:
            logger.warning("Content filter blocked a %s call; stopping this loop", tag.component)
            return _stop(result, "content_filter", messages)
        result.turns += 1
        result.input_tokens += reply.usage.input_tokens
        result.output_tokens += reply.usage.output_tokens
        if reply.content or reply.tool_calls:
            messages.append(reply.assistant_message)
        if reply.content:
            _emit(on_event, "agent", {"text": reply.content})

        if not reply.tool_calls:
            if reply.finish_reason == "length":
                logger.info("Reply cut off by max_completion_tokens (turn %d)", result.turns)
                if not reply.content:
                    messages.append({"role": "user", "content": LENGTH_NUDGE})
                continue
            result.final_text = reply.content
            return _stop(result, "final", messages)

        for call in reply.tool_calls:
            _emit(on_event, "tool_call", {"name": call.name, "arguments": call.arguments})
            invalid = _invalid_arguments_error(call)
            if invalid is None and call.name in terminal_names:
                result.terminal_call = call
                return _stop(result, "terminal", messages)
            text = invalid if invalid is not None else tools.dispatch(call)
            _emit(
                on_event,
                "tool_result",
                {"name": call.name, "text": text, "error": text.startswith(ERROR_PREFIX)},
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    return _stop(result, "max_turns", messages)


def _close_unanswered_calls(
    history: Sequence[dict[str, Any]], terminal_names: set[str]
) -> list[dict[str, Any]]:
    """Tool messages for the calls of the last assistant message that have no output yet.

    The Responses API rejects a function call without an output, so a terminal call gets
    `SUBMITTED_OUTPUT` and every other call `CLOSED_OUTPUT`, in call order.
    """
    last = next(
        (i for i in range(len(history) - 1, -1, -1) if history[i].get("role") == "assistant"),
        None,
    )
    if last is None:
        return []
    answered = {m["tool_call_id"] for m in history[last + 1 :] if m.get("role") == "tool"}
    return [
        {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": (
                SUBMITTED_OUTPUT if call["function"]["name"] in terminal_names else CLOSED_OUTPUT
            ),
        }
        for call in history[last].get("tool_calls") or []
        if call["id"] not in answered
    ]


def run_forced_turn(
    client: LLMClient,
    *,
    model: str,
    reasoning_effort: str,
    system: str,
    history: list[dict[str, Any]],
    user: str,
    tools: ToolSet,
    terminal_tools: Sequence[dict[str, Any]],
    forced_tool: str,
    max_completion_tokens: int,
    max_input_tokens_per_call: int,
    tag: CostTag,
    on_event: EventHandler | None = None,
) -> str | None:
    """Continue a finished loop's conversation with one call that must use `forced_tool`.

    Every call of the last assistant message in `history` that has no output yet gets one
    (`SUBMITTED_OUTPUT` for terminal tools, `CLOSED_OUTPUT` otherwise), then `user` is added.
    The tools offered are `[*tools.schemas(), *terminal_tools]`, the same list and order as
    `run_tool_loop`, so the prompt prefix stays cacheable. Only the first returned call to
    `forced_tool` is dispatched; other calls in the reply are ignored. Events are those of
    `run_tool_loop` for that one call.

    Returns
    -------
    str | None
        The dispatched tool's result (an `ERROR:` text for unparseable arguments), or None when
        the prompt exceeds `max_input_tokens_per_call` (no call is made), the content filter
        rejects the request, or the reply has no call to `forced_tool`.

    Raises
    ------
    ValueError
        If `forced_tool` is not one of the tools in `tools`.
    """
    tool_names = {_tool_name(schema) for schema in tools.schemas()}
    if forced_tool not in tool_names:
        raise ValueError(f"forced tool {forced_tool!r} is not in the tool set")
    schemas = [*tools.schemas(), *terminal_tools]
    terminal_names = {_tool_name(schema) for schema in terminal_tools}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        *history,
        *_close_unanswered_calls(history, terminal_names),
        {"role": "user", "content": user},
    ]
    prompt_tokens = count_chat_tokens(messages, schemas)
    if prompt_tokens > max_input_tokens_per_call:
        logger.info(
            "Forced %s turn skipped: prompt of %d tokens exceeds the %d-token limit",
            forced_tool,
            prompt_tokens,
            max_input_tokens_per_call,
        )
        return None
    try:
        reply = client.chat(
            model=model,
            messages=messages,
            tools=schemas,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
            tag=tag,
            tool_choice={"type": "function", "name": forced_tool},
        )
    except ContentFiltered:
        logger.warning("Content filter blocked a forced %s turn; skipping it", forced_tool)
        return None
    if reply.content:
        _emit(on_event, "agent", {"text": reply.content})
    forced_calls = [call for call in reply.tool_calls if call.name == forced_tool]
    if len(forced_calls) != len(reply.tool_calls):
        logger.warning(
            "Forced %s turn returned %d other calls; they were not run",
            forced_tool,
            len(reply.tool_calls) - len(forced_calls),
        )
    if not forced_calls:
        logger.warning("Forced %s turn returned no call to that tool", forced_tool)
        return None
    call = forced_calls[0]
    _emit(on_event, "tool_call", {"name": call.name, "arguments": call.arguments})
    invalid = _invalid_arguments_error(call)
    text = invalid if invalid is not None else tools.dispatch(call)
    _emit(
        on_event,
        "tool_result",
        {"name": call.name, "text": text, "error": text.startswith(ERROR_PREFIX)},
    )
    return text
