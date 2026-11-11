"""Arms that keep a memory directory and consolidate it after each session: A1, A3 and A4.

A1 (rolling summary) keeps `summary.md`: a review phase that reads the transcripts verifies,
corrects and enriches it in place, then a rewrite phase without transcripts compacts it and
rewrites `injected.md`. A3 (organize-only) and A4 (verified dreaming) keep a wiki fed by the
notes the online agent writes under `notes/`; A3 only organizes it, A4 first verifies it
against the session's transcript. A1's two phases mirror A4's analyzer and organizer: the same
turns, transcript access, core prompt, input budget and reasoning effort.

Follow-up switches add to this, and leave it unchanged when off: with `session_notes`, A1's
agent may also write notes under `notes/`, and A1's review and A4's analyzer get the shared
notes rule (A1's review also merges the notes into `summary.md` and retires them); with
`consolidator_sql`, A4's analyzer may query the database of the session that has just ended.
"""

import logging
import re
from abc import ABC, abstractmethod
from pathlib import PurePosixPath
from typing import Final

from project_module.agent.context import Arm, ArmContext
from project_module.config import ArmName, ConsolidationBudget, ExperimentConfig
from project_module.consolidation import (
    INJECTED_FILE,
    LintProfile,
    PhaseSpec,
    require_memory,
    run_consolidation,
)
from project_module.contracts import ConsolidationRecord, TaskQuestion, ToolSet
from project_module.memory.store import MemoryStore
from project_module.memory.tools import MemoryReadTools
from project_module.schemadrift.models import InjectedNote

logger = logging.getLogger(__name__)

SUMMARY_FILE: Final = "summary.md"
NOTES_PREFIX: Final = "notes/"

CORE_PROMPT: Final = "_consolidation_core.md"
REVIEW_PROMPT: Final = "rolling_review.md"
REWRITE_PROMPT: Final = "rolling_rewrite.md"
ANALYZER_PROMPT: Final = "analyzer_verify.md"
ORGANIZER_PROMPT: Final = "organizer.md"
# Fragments appended after an arm's prompts when a follow-up switch is on.
NOTES_HANDLING_PROMPT: Final = "notes_handling.md"
REVIEW_NOTES_PROMPT: Final = "rolling_review_notes.md"
SQL_PROBE_PROMPT: Final = "consolidator_sql_addendum.md"

_NOT_SLUG: Final = re.compile(r"[^a-z0-9]+")
_NOTE_NAME_WORDS: Final = 6


class _MemoryArm(Arm, ABC):
    """What the memory arms share: injection of `injected.md`, memory tools, consolidation."""

    name: ArmName
    lint_profile: LintProfile
    # Directory the online agent may write notes below; None keeps memory read-only online.
    notes_prefix: str | None

    @abstractmethod
    def phases(self, ctx: ArmContext) -> list[PhaseSpec]:
        """The consolidation phases this arm runs after a session, in order, for `ctx.config`."""

    def setup(self, ctx: ArmContext) -> None:
        require_memory(ctx)

    def injected_context(self, ctx: ArmContext, session: int) -> str:
        memory = require_memory(ctx)
        return memory.read_all(INJECTED_FILE) if memory.exists(INJECTED_FILE) else ""

    def online_tools(self, ctx: ArmContext, session: int) -> ToolSet:
        return self._memory_tools(ctx, writable_prefix=self._online_notes_prefix(ctx.config))

    def question_context(self, ctx: ArmContext, question: TaskQuestion) -> str:
        return ""

    def probe_tools(self, ctx: ArmContext, session: int) -> ToolSet:
        return self._memory_tools(ctx, writable_prefix=None)

    def after_session(self, ctx: ArmContext, session: int) -> ConsolidationRecord | None:
        if session >= ctx.env.sessions:
            logger.info(
                "%s/%s: no consolidation after the last session %d",
                ctx.stream_id,
                self.name,
                session,
            )
            return None
        return run_consolidation(
            ctx,
            self.name,
            session=session,
            phases=self.phases(ctx),
            lint_profile=self.lint_profile,
        )

    def _online_notes_prefix(self, config: ExperimentConfig) -> str | None:
        """Where the online agent may write notes under `config`; None for nowhere."""
        return self.notes_prefix

    @staticmethod
    def _memory_tools(ctx: ArmContext, *, writable_prefix: str | None) -> ToolSet:
        return MemoryReadTools(
            require_memory(ctx),
            token_budget=ctx.config.budgets.online.memory_read_tokens,
            writable_prefix=writable_prefix,
        )


class RollingSummaryArm(_MemoryArm):
    """A1: review `summary.md` against the transcript, then compact it and rewrite `injected.md`."""

    name: ArmName = "A1"
    lint_profile: LintProfile = "summary"
    # Without `session_notes`; with it, the agent writes one note per question under `notes/`.
    notes_prefix = None

    def phases(self, ctx: ArmContext) -> list[PhaseSpec]:
        budget = ctx.config.budgets.consolidation
        notes = [NOTES_HANDLING_PROMPT, REVIEW_NOTES_PROMPT] if ctx.config.session_notes else []
        return [
            PhaseSpec(
                name="summarizer_review",
                prompt_files=[CORE_PROMPT, REVIEW_PROMPT, *notes],
                max_turns=budget.analyzer_max_turns,
                transcripts_readable=True,
            ),
            PhaseSpec(
                name="summarizer_rewrite",
                prompt_files=[REWRITE_PROMPT],
                max_turns=budget.organizer_max_turns,
                transcripts_readable=False,
            ),
        ]

    def _online_notes_prefix(self, config: ExperimentConfig) -> str | None:
        return NOTES_PREFIX if config.session_notes else self.notes_prefix

    def inject_false_note(
        self, ctx: ArmContext, note: InjectedNote, *, expose: bool = False
    ) -> None:
        """Append the note to the end of `summary.md`, creating the file if needed."""
        memory = require_memory(ctx)
        _require_marker(note)
        _append_to(memory, SUMMARY_FILE, note.text)
        if expose:
            _prepend_to(memory, INJECTED_FILE, note.text)
        logger.info(
            "%s/%s: false note on %s appended to %s",
            ctx.stream_id,
            self.name,
            note.fact_id,
            SUMMARY_FILE,
        )


class _WikiArm(_MemoryArm):
    """A3 and A4: the online agent writes notes under `notes/`, consolidation keeps a wiki."""

    lint_profile: LintProfile = "wiki"
    notes_prefix = NOTES_PREFIX

    def inject_false_note(
        self, ctx: ArmContext, note: InjectedNote, *, expose: bool = False
    ) -> None:
        """Write the note as a new file under `notes/`, named after its first line."""
        memory = require_memory(ctx)
        _require_marker(note)
        slug = _unused_slug(memory, _note_slug(note.text))
        path = f"{NOTES_PREFIX}{slug}.md"
        memory.write(path, note.text.strip() + "\n")
        if expose:
            _prepend_to(memory, INJECTED_FILE, note.text)
        logger.info(
            "%s/%s: false note on %s written to %s", ctx.stream_id, self.name, note.fact_id, path
        )


class OrganizeOnlyArm(_WikiArm):
    """A3: an organizer tidies the notes into the wiki without reading transcripts."""

    name: ArmName = "A3"

    def phases(self, ctx: ArmContext) -> list[PhaseSpec]:
        return [_organizer_phase(ctx.config.budgets.consolidation)]


class VerifiedDreamingArm(_WikiArm):
    """A4: an analyzer verifies memory against the transcript, then the organizer tidies it."""

    name: ArmName = "A4"

    def phases(self, ctx: ArmContext) -> list[PhaseSpec]:
        config = ctx.config
        budget = config.budgets.consolidation
        fragments = [NOTES_HANDLING_PROMPT] if config.session_notes else []
        fragments += [SQL_PROBE_PROMPT] if config.consolidator_sql else []
        return [
            PhaseSpec(
                name="analyzer",
                prompt_files=[CORE_PROMPT, ANALYZER_PROMPT, *fragments],
                max_turns=budget.analyzer_max_turns,
                transcripts_readable=True,
                sql_probe=config.consolidator_sql,
            ),
            _organizer_phase(budget),
        ]


def _organizer_phase(budget: ConsolidationBudget) -> PhaseSpec:
    return PhaseSpec(
        name="organizer",
        prompt_files=[ORGANIZER_PROMPT],
        max_turns=budget.organizer_max_turns,
        transcripts_readable=False,
    )


def _require_marker(note: InjectedNote) -> None:
    # Survival of a false note is measured by its marker, so a note without it is unusable.
    if note.marker not in note.text:
        raise ValueError(f"the false note on {note.fact_id} does not contain its marker")


def _prepend_to(memory: MemoryStore, path: str, paragraph: str) -> None:
    """Put `paragraph` first: the injected layer is truncated from the end."""
    current = memory.read_all(path) if memory.exists(path) else ""
    memory.write(
        path, _append_paragraph(paragraph, current) if current.strip() else paragraph.strip() + "\n"
    )


def _append_to(memory: MemoryStore, path: str, paragraph: str) -> None:
    current = memory.read_all(path) if memory.exists(path) else ""
    memory.write(path, _append_paragraph(current, paragraph))


def _append_paragraph(text: str, paragraph: str) -> str:
    if not text.strip():
        return paragraph.strip() + "\n"
    return f"{text.rstrip()}\n\n{paragraph.strip()}\n"


def _note_slug(text: str) -> str:
    """A file name in the agent's style: the first words of the note's first non-empty line."""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    words = _NOT_SLUG.sub(" ", first_line.lower()).split()[:_NOTE_NAME_WORDS]
    if not words:
        raise ValueError(f"cannot derive a file name from the note {text!r}")
    return "-".join(words)


def _unused_slug(store: MemoryStore, base: str) -> str:
    """`base`, or `base-2`, `base-3`, ... whichever no file in the store uses as its stem.

    Slugs name pages in `[[slug]]` links, so they must be unique across the whole store.
    """
    taken = {PurePosixPath(path).stem for path in store.glob("**/*")}
    slug, suffix = base, 2
    while slug in taken:
        slug, suffix = f"{base}-{suffix}", suffix + 1
    return slug
