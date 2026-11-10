"""Offline memory consolidation between sessions: phases of file-tool loops over the memory.

A phase is one `run_tool_loop` conversation of the consolidator model with the five file
tools of `FileToolset`. The memory directory is always mounted read-write as ``memory``; the
job's transcripts are mounted read-only as ``transcripts`` only for phases allowed to read them.
"""

import logging
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, Field

from project_module.agent.context import ArmContext
from project_module.config import ArmName, Budgets
from project_module.contracts import ConsolidationRecord
from project_module.llm.loop import run_tool_loop
from project_module.memory.lint import lint_store
from project_module.memory.store import MemoryStore, false_notes_present
from project_module.memory.tools import MAX_READ_RESULT_TOKENS, FileToolset
from project_module.prompts import render_prompt

logger = logging.getLogger(__name__)

MEMORY_MOUNT: Final = "memory"
TRANSCRIPTS_MOUNT: Final = "transcripts"
INJECTED_FILE: Final = "injected.md"

# Conservative words-per-token ratio, so that a model writing to the word guide stays in budget.
_WORDS_PER_TOKEN: Final = 0.6

LintProfile = Literal["wiki", "summary"]


class PhaseSpec(BaseModel):
    """One consolidation phase: who runs, with which prompt, for how long, seeing what."""

    name: Literal["summarizer_review", "summarizer_rewrite", "analyzer", "organizer"]
    # File names under `prompts/`, concatenated in order into the system prompt.
    prompt_files: list[str] = Field(min_length=1)
    max_turns: int = Field(ge=1)
    transcripts_readable: bool


def require_memory(ctx: ArmContext) -> MemoryStore:
    """The memory store of an arm that keeps memory.

    Raises
    ------
    ValueError
        If the runner gave this arm no memory store.
    """
    if ctx.memory is None:
        raise ValueError(f"stream {ctx.stream_id}: a memory arm needs a memory store")
    return ctx.memory


def prompt_values(budgets: Budgets) -> dict[str, int]:
    """The budget figures the consolidation prompts quote, by placeholder name."""
    return {
        "injected_tokens": budgets.injected_tokens,
        "injected_words": int(budgets.injected_tokens * _WORDS_PER_TOKEN),
        "memory_read_tokens": budgets.online.memory_read_tokens,
        "max_output_tokens": budgets.consolidation.max_completion_tokens,
        "tool_result_tokens": MAX_READ_RESULT_TOKENS,
    }


def run_consolidation(
    ctx: ArmContext,
    arm: ArmName,
    *,
    session: int,
    phases: Sequence[PhaseSpec],
    lint_profile: LintProfile,
) -> ConsolidationRecord:
    """Consolidate the memory after `session` by running `phases` in order.

    All phases together may use at most `budgets.consolidation.max_input_tokens_per_run`
    input tokens: each phase gets what the earlier ones left. Afterwards the store is linted
    and searched for the stream's false notes (marker or claim), and the record says whether
    `injected.md` changed.

    Raises
    ------
    ValueError
        If the context has no memory store.
    FileNotFoundError
        If a phase may read transcripts but the session's transcript does not exist.
    """
    memory = require_memory(ctx)
    budgets = ctx.config.budgets
    transcript = ctx.layout.transcript_file(ctx.stream_id, arm, session).name
    if any(phase.transcripts_readable for phase in phases) and not ctx.transcripts.exists(
        transcript
    ):
        raise FileNotFoundError(f"no transcript {transcript} for session {session}")
    values = prompt_values(budgets)
    injected_before = _read_injected(memory)
    remaining = budgets.consolidation.max_input_tokens_per_run
    turns = input_tokens = output_tokens = 0
    stops: list[str] = []
    for phase in phases:
        mounts = {MEMORY_MOUNT: memory}
        if phase.transcripts_readable:
            mounts[TRANSCRIPTS_MOUNT] = ctx.transcripts
        logger.info(
            "%s/%s session %d: consolidation phase %s starts (%d turns, %d input tokens left)",
            ctx.stream_id,
            arm,
            session,
            phase.name,
            phase.max_turns,
            remaining,
        )
        result = run_tool_loop(
            ctx.client,
            model=ctx.config.consolidator_model,
            reasoning_effort=ctx.config.consolidator_reasoning_effort,
            system=render_prompt(phase.prompt_files, values),
            user=_task_message(phase, session=session, transcript=transcript),
            tools=FileToolset(mounts),
            max_turns=phase.max_turns,
            max_completion_tokens=budgets.consolidation.max_completion_tokens,
            max_input_tokens_per_call=budgets.max_input_tokens_per_call,
            max_total_input_tokens=remaining,
            tag=ctx.tag(arm, session, "consolidation"),
        )
        remaining = max(remaining - result.input_tokens, 0)
        turns += result.turns
        input_tokens += result.input_tokens
        output_tokens += result.output_tokens
        stops.append(f"{phase.name}:{result.stopped}")
        logger.info(
            "%s/%s session %d: phase %s stopped (%s) after %d turns",
            ctx.stream_id,
            arm,
            session,
            phase.name,
            result.stopped,
            result.turns,
        )

    problems = lint_store(
        memory, profile=lint_profile, injected_token_limit=budgets.injected_tokens
    )
    if problems:
        logger.warning(
            "%s/%s session %d: memory has %d lint problems after consolidation",
            ctx.stream_id,
            arm,
            session,
            len(problems),
        )
    injected_after = _read_injected(memory)
    notes = ctx.stream.injections if ctx.stream is not None else []
    return ConsolidationRecord(
        stream_id=ctx.stream_id,
        arm=arm,
        session=session,
        phases=[phase.name for phase in phases],
        turns=turns,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stopped=";".join(stops),
        lint_problems=problems,
        injected_markers_present=false_notes_present(memory, notes),
        injected_updated=injected_after is not None and injected_after != injected_before,
    )


def _read_injected(memory: MemoryStore) -> str | None:
    return memory.read_all(INJECTED_FILE) if memory.exists(INJECTED_FILE) else None


def _task_message(phase: PhaseSpec, *, session: int, transcript: str) -> str:
    """The user turn of a phase; identical in form for every arm, so only the prompts differ."""
    lines = [f"Session {session} has just ended."]
    if phase.transcripts_readable:
        lines.append(
            f"Its transcript is `{TRANSCRIPTS_MOUNT}/{transcript}`; the transcripts of earlier "
            "sessions are in the same directory."
        )
    lines.append(
        f"Update the memory directory `{MEMORY_MOUNT}/` as your instructions describe. You have "
        f"at most {phase.max_turns} turns. A reply without a tool call ends your run, so reply "
        "in plain text only when you are done, with one short paragraph on what you changed."
    )
    return "\n".join(lines)
