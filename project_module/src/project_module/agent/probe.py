"""Memory probes: short questions about the database answered from memory alone."""

import logging
from typing import Any, Final

from project_module.agent.context import Arm, ArmContext
from project_module.agent.session import CombinedToolSet, injected_layer, system_prompt
from project_module.contracts import ProbeResult, ToolSet
from project_module.llm.loop import LoopResult, run_tool_loop
from project_module.prompts import load_prompt
from project_module.schemadrift.models import Probe
from project_module.schemadrift.probes import make_probes, probe_correct
from project_module.tool_names import MEMORY_WRITE_TOOL

logger = logging.getLogger(__name__)

ANSWER_TOOL: Final = "answer"
_WRITE_TOOLS: Final = frozenset({MEMORY_WRITE_TOOL, "write_file", "edit_file"})
_ANSWER_SCHEMA: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": ANSWER_TOOL,
        "description": (
            "Give your final answer: only the exact value asked for, such as a table or column "
            "name, a code, or a unit, with no explanation. Call this exactly once."
        ),
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string", "description": "The exact value."}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
}


def _read_only(tools: ToolSet | None) -> list[ToolSet]:
    if tools is None:
        return []
    names = {str(schema["function"]["name"]) for schema in tools.schemas()}
    if writes := names & _WRITE_TOOLS:
        raise ValueError(f"probe tools must not write memory, got {sorted(writes)}")
    return [tools]


def _answer(loop: LoopResult) -> str | None:
    if loop.terminal_call is None:
        return None
    value = loop.terminal_call.arguments.get("answer")
    return value if isinstance(value, str) else None


def _ask(arm: Arm, ctx: ArmContext, session: int, probe: Probe, system: str) -> ProbeResult:
    budgets = ctx.config.budgets
    loop = run_tool_loop(
        ctx.client,
        model=ctx.config.agent_model,
        reasoning_effort=ctx.config.agent_reasoning_effort,
        system=system,
        user=probe.question,
        tools=CombinedToolSet(_read_only(arm.probe_tools(ctx, session))),
        terminal_tools=[_ANSWER_SCHEMA],
        max_turns=budgets.online.max_turns,
        max_completion_tokens=budgets.online.max_completion_tokens,
        max_input_tokens_per_call=budgets.max_input_tokens_per_call,
        max_total_input_tokens=None,
        tag=ctx.tag(arm.name, session, "probe"),
        force_terminal_on_last_turn=True,
    )
    answer = _answer(loop)
    return ProbeResult(
        stream_id=ctx.stream_id,
        arm=arm.name,
        session=session,
        fact_id=probe.fact_id,
        injected=probe.injected,
        correct=answer is not None and probe_correct(probe, answer),
    )


def run_probes(arm: Arm, ctx: ArmContext, session: int) -> list[ProbeResult]:
    """Ask the probes of `session` using only the arm's injected layer and read-only tools.

    Probes have no database tool and write no transcript. An arm with neither an injected
    layer nor probe tools (A0) has no memory to probe and is skipped. `BudgetExceeded`
    propagates to the caller.

    Raises
    ------
    ValueError
        If the context has no SchemaDrift stream, or the arm's probe tools can write memory.
    """
    if ctx.stream is None:
        raise ValueError("probes need a SchemaDrift stream")
    injected = injected_layer(arm, ctx, session)
    if not injected.strip() and arm.probe_tools(ctx, session) is None:
        logger.info("%s %s: no memory to probe at session %d", ctx.stream_id, arm.name, session)
        return []
    system = system_prompt(load_prompt("probe_system.md"), injected)
    probes = make_probes(ctx.stream, session, ctx.config.probes_per_checkpoint)
    results = [_ask(arm, ctx, session, probe, system) for probe in probes]
    logger.info(
        "%s %s: %d probes asked at session %d", ctx.stream_id, arm.name, len(results), session
    )
    logger.debug(
        "%s %s probes at session %d: %d/%d correct",
        ctx.stream_id,
        arm.name,
        session,
        sum(result.correct for result in results),
        len(results),
    )
    return results
