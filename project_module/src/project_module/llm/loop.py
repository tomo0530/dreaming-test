"""Bounded tool-calling loop shared by the online agent, probes and consolidation phases."""

import logging
from collections.abc import Callable, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel

from project_module.contracts import CostTag, LLMClient, ToolCall, ToolSet
from project_module.llm import INVALID_ARGUMENTS_KEY, ContentFiltered
from project_module.tokens import count_chat_tokens

logger = logging.getLogger(__name__)

ERROR_PREFIX: Final = "ERROR:"

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


def _stop(result: LoopResult, stopped: LoopStop) -> LoopResult:
    result.stopped = stopped
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
    return f"{ERROR_PREFIX} arguments are not valid JSON objects: {raw}"


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
) -> LoopResult:
    """Run one conversation until a terminal tool, a plain answer, or a budget stops it.

    A turn is one model call. Before each call the prompt is counted locally; the loop stops
    without calling when that count exceeds `max_input_tokens_per_call`, or when billed input
    so far plus that count exceeds `max_total_input_tokens`. The tool calls of one reply are
    handled in order; a call to a terminal tool ends the loop and is returned unexecuted.
    Calls whose arguments are not a JSON object get an `ERROR:` text instead of dispatch, and
    a terminal call in that state does not end the loop. A reply cut off by
    `finish_reason == "length"` without tool calls uses up its turn and the loop goes on.

    Events passed to `on_event`: `("agent", {"text"})` for non-empty reply text,
    `("tool_call", {"name", "arguments"})` for every call including the terminal one, and
    `("tool_result", {"name", "text", "error"})` for every non-terminal call.

    Raises
    ------
    ValueError
        If `max_turns` is less than 1.
    """
    if max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    schemas = [*tools.schemas(), *terminal_tools]
    terminal_names = {_tool_name(schema) for schema in terminal_tools}
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
    )
    while result.turns < max_turns:
        prompt_tokens = count_chat_tokens(messages, schemas or None)
        if prompt_tokens > max_input_tokens_per_call:
            return _stop(result, "call_input_budget")
        if (
            max_total_input_tokens is not None
            and result.input_tokens + prompt_tokens > max_total_input_tokens
        ):
            return _stop(result, "total_input_budget")

        try:
            reply = client.chat(
                model=model,
                messages=messages,
                tools=schemas or None,
                reasoning_effort=reasoning_effort,
                max_completion_tokens=max_completion_tokens,
                tag=tag,
            )
        except ContentFiltered:
            logger.warning("Content filter blocked a %s call; stopping this loop", tag.component)
            return _stop(result, "content_filter")
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
                continue
            result.final_text = reply.content
            return _stop(result, "final")

        for call in reply.tool_calls:
            _emit(on_event, "tool_call", {"name": call.name, "arguments": call.arguments})
            invalid = _invalid_arguments_error(call)
            if invalid is None and call.name in terminal_names:
                result.terminal_call = call
                return _stop(result, "terminal")
            text = invalid if invalid is not None else tools.dispatch(call)
            _emit(
                on_event,
                "tool_result",
                {"name": call.name, "text": text, "error": text.startswith(ERROR_PREFIX)},
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    return _stop(result, "max_turns")
