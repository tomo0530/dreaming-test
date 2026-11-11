"""Offline memory consolidation between sessions: phases of file-tool loops over the memory.

A phase is one `run_tool_loop` conversation of the consolidator model with the five file
tools of `FileToolset`. The memory directory is always mounted read-write as ``memory``; the
job's transcripts are mounted read-only as ``transcripts`` only for phases allowed to read them.
A phase with `sql_probe` also holds the `query` tool of `consolidator_query_tool` on the
database of the session that has just ended.
"""

import logging
import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from project_module.agent.context import ArmContext
from project_module.agent.session import CombinedToolSet, QueryTool
from project_module.config import ArmName, Budgets
from project_module.contracts import ConsolidationRecord, TaskEnv, ToolSet
from project_module.llm.loop import ERROR_PREFIX, run_tool_loop
from project_module.memory.lint import lint_store
from project_module.memory.store import MemoryStore, false_notes_present
from project_module.memory.tools import MAX_READ_RESULT_TOKENS, FileToolset
from project_module.prompts import render_prompt
from project_module.tool_names import QUERY_TOOL

logger = logging.getLogger(__name__)

MEMORY_MOUNT: Final = "memory"
TRANSCRIPTS_MOUNT: Final = "transcripts"
INJECTED_FILE: Final = "injected.md"
NOTES_DIR: Final = "notes"
# Queries one consolidation may run with `consolidator_query_tool` (00_design §4.5).
CONSOLIDATOR_MAX_QUERIES: Final = 10
# A note retired by consolidation: its whole content is one line in one of these forms.
_RETIRED_NOTE: Final = re.compile(r"\A(?:MOVED TO|MERGED INTO)\b[^\n]*\Z")
# A body line of a `read_file` result: its 1-based line number, a tab, then the line.
_NUMBERED_LINE: Final = re.compile(r"^\d+\t(.*)$", re.MULTILINE)

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
    # Whether the phase may query the database of the session that has just ended.
    sql_probe: bool = False


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
        "query_row_limit": budgets.online.sql_row_limit,
        "consolidator_max_queries": CONSOLIDATOR_MAX_QUERIES,
    }


def consolidator_query_tool(
    env: TaskEnv, session: int, *, max_queries: int = CONSOLIDATOR_MAX_QUERIES
) -> QueryTool:
    """The `query(sql)` tool of a consolidation phase, on the session that has just ended.

    Queries run like the agent's (`env.run_query(session, sql)`, with the same row and time
    limits) on the database of `session`, never on a later one. At most `max_queries` are run
    per tool; later attempts are not run and return an `ERROR:` text. Calls without a usable
    `sql` argument are rejected without counting.

    Raises
    ------
    ValueError
        If `max_queries` is below 1.
    """
    if max_queries < 1:
        raise ValueError("max_queries must be at least 1")
    return QueryTool(
        env,
        session,
        max_queries=max_queries,
        description=(
            "Run one read-only SQL statement against the database as it is in the "
            "session that has just ended, and return the result as text, or an "
            "error message starting with `ERROR:`. Long results are cut to a row "
            "limit, so use aggregates, filters or LIMIT. You may run at most "
            f"{max_queries} queries in this run; further calls are not run."
        ),
        limit_error=f"query limit reached ({max_queries} per consolidation); the query was not run.",
    )


class _ConsolidationCounter:
    """Loop event handler: counts reads of live notes and executed queries across phases.

    A read counts when `read_file` on a file below `memory/notes/` returned at least one
    non-blank line of the file and the file is not a retired one-line stub at that moment.
    A query counts when it ran and its result is not an `ERROR:` text.
    """

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory
        self._arguments: dict[str, Any] = {}
        self.notes_files_read = 0
        self.sql_queries = 0

    def handle_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "agent":
            return
        if kind == "tool_call":
            # Consolidation has no terminal tools, so a result follows every call.
            self._arguments = dict(data["arguments"])
            return
        if kind != "tool_result":
            raise ValueError(f"unknown loop event: {kind!r}")
        name, text = str(data["name"]), str(data["text"])
        if text.startswith(ERROR_PREFIX):
            return
        if name == QUERY_TOOL:
            self.sql_queries += 1
        elif name == "read_file" and self._is_live_note_read(text):
            self.notes_files_read += 1

    def _is_live_note_read(self, text: str) -> bool:
        path = self._arguments.get("path")
        if not isinstance(path, str):
            return False
        parts = PurePosixPath(path).parts
        if len(parts) < 3 or parts[:2] != (MEMORY_MOUNT, NOTES_DIR):
            return False
        if not any(line.strip() for line in _NUMBERED_LINE.findall(text)):
            return False
        rel = "/".join(parts[1:])
        return self._memory.exists(rel) and not _is_retired_note(self._memory.read_all(rel))


def _is_retired_note(content: str) -> bool:
    return _RETIRED_NOTE.match(content.strip()) is not None


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
    `injected.md` changed. Phases with `sql_probe` also get `consolidator_query_tool` on
    `session`; one instance, and so one query limit, serves every such phase of the run. The
    record counts reads of live notes and executed queries over all phases.

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
    counter = _ConsolidationCounter(memory)
    query_tool = (
        consolidator_query_tool(ctx.env, session) if any(p.sql_probe for p in phases) else None
    )
    for phase in phases:
        mounts = {MEMORY_MOUNT: memory}
        if phase.transcripts_readable:
            mounts[TRANSCRIPTS_MOUNT] = ctx.transcripts
        file_tools = FileToolset(mounts)
        tools: ToolSet = (
            CombinedToolSet([file_tools, query_tool])
            if phase.sql_probe and query_tool is not None
            else file_tools
        )
        logger.info(
            "%s/%s session %d: consolidation phase %s starts (%d turns, %d input tokens left%s)",
            ctx.stream_id,
            arm,
            session,
            phase.name,
            phase.max_turns,
            remaining,
            ", with query" if phase.sql_probe else "",
        )
        result = run_tool_loop(
            ctx.client,
            model=ctx.config.consolidator_model,
            reasoning_effort=ctx.config.consolidator_reasoning_effort,
            system=render_prompt(phase.prompt_files, values),
            user=_task_message(phase, session=session, transcript=transcript),
            tools=tools,
            max_turns=phase.max_turns,
            max_completion_tokens=budgets.consolidation.max_completion_tokens,
            max_input_tokens_per_call=budgets.max_input_tokens_per_call,
            max_total_input_tokens=remaining,
            tag=ctx.tag(arm, session, "consolidation"),
            on_event=counter.handle_event,
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

    logger.info(
        "%s/%s session %d: consolidation read live notes %d times and ran %d queries",
        ctx.stream_id,
        arm,
        session,
        counter.notes_files_read,
        counter.sql_queries,
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
        notes_files_read=counter.notes_files_read,
        sql_queries=counter.sql_queries,
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
