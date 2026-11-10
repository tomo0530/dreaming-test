"""Arms that keep a memory directory and consolidate it after each session: A1, A3 and A4.

A1 (rolling summary) keeps `summary.md`, rewritten by one summarizer phase that reads the
transcripts. A3 (organize-only) and A4 (verified dreaming) keep a wiki fed by the notes the
online agent writes under `notes/`; A3 only organizes it, A4 first verifies it against the
session's transcript. A1's single phase gets as many turns as A4's two phases together, and
both share the same core prompt, input budget and reasoning effort.
"""

import logging
import re
from abc import ABC, abstractmethod
from pathlib import PurePosixPath
from typing import Final

from project_module.agent.context import Arm, ArmContext
from project_module.config import ArmName, ConsolidationBudget
from project_module.consolidation import (
    LintProfile,
    PhaseSpec,
    require_memory,
    run_consolidation,
)
from project_module.contracts import ConsolidationRecord, ToolSet
from project_module.memory.store import MemoryStore
from project_module.memory.tools import MemoryReadTools
from project_module.schemadrift.models import InjectedNote

logger = logging.getLogger(__name__)

INJECTED_FILE: Final = "injected.md"
SUMMARY_FILE: Final = "summary.md"
NOTES_PREFIX: Final = "notes/"

CORE_PROMPT: Final = "_consolidation_core.md"
SUMMARIZER_PROMPT: Final = "rolling_summarizer.md"
ANALYZER_PROMPT: Final = "analyzer_verify.md"
ORGANIZER_PROMPT: Final = "organizer.md"

_NOT_SLUG: Final = re.compile(r"[^a-z0-9]+")
_NOTE_NAME_WORDS: Final = 6


class _MemoryArm(Arm, ABC):
    """What the memory arms share: injection of `injected.md`, memory tools, consolidation."""

    name: ArmName
    lint_profile: LintProfile
    # Directory the online agent may write notes below; None keeps memory read-only online.
    notes_prefix: str | None

    @abstractmethod
    def phases(self, budget: ConsolidationBudget) -> list[PhaseSpec]:
        """The consolidation phases this arm runs after a session, in order."""

    def setup(self, ctx: ArmContext) -> None:
        require_memory(ctx)

    def injected_context(self, ctx: ArmContext, session: int) -> str:
        memory = require_memory(ctx)
        return memory.read_all(INJECTED_FILE) if memory.exists(INJECTED_FILE) else ""

    def online_tools(self, ctx: ArmContext, session: int) -> ToolSet:
        return self._memory_tools(ctx, writable_prefix=self.notes_prefix)

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
            phases=self.phases(ctx.config.budgets.consolidation),
            lint_profile=self.lint_profile,
        )

    @staticmethod
    def _memory_tools(ctx: ArmContext, *, writable_prefix: str | None) -> ToolSet:
        return MemoryReadTools(
            require_memory(ctx),
            token_budget=ctx.config.budgets.online.memory_read_tokens,
            writable_prefix=writable_prefix,
        )


class RollingSummaryArm(_MemoryArm):
    """A1: a summarizer rewrites `summary.md` and `injected.md` from the transcripts."""

    name: ArmName = "A1"
    lint_profile: LintProfile = "summary"
    notes_prefix = None

    def phases(self, budget: ConsolidationBudget) -> list[PhaseSpec]:
        return [
            PhaseSpec(
                name="summarizer",
                prompt_files=[CORE_PROMPT, SUMMARIZER_PROMPT],
                max_turns=budget.summarizer_max_turns,
                transcripts_readable=True,
            )
        ]

    def inject_false_note(self, ctx: ArmContext, note: InjectedNote) -> None:
        """Append the note to the end of `summary.md`, creating the file if needed."""
        memory = require_memory(ctx)
        _require_marker(note)
        current = memory.read_all(SUMMARY_FILE) if memory.exists(SUMMARY_FILE) else ""
        memory.write(SUMMARY_FILE, _append_paragraph(current, note.text))
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

    def inject_false_note(self, ctx: ArmContext, note: InjectedNote) -> None:
        """Write the note as a new file under `notes/`, named after its first line."""
        memory = require_memory(ctx)
        _require_marker(note)
        slug = _unused_slug(memory, _note_slug(note.text))
        path = f"{NOTES_PREFIX}{slug}.md"
        memory.write(path, note.text.strip() + "\n")
        logger.info(
            "%s/%s: false note on %s written to %s", ctx.stream_id, self.name, note.fact_id, path
        )


class OrganizeOnlyArm(_WikiArm):
    """A3: an organizer tidies the notes into the wiki without reading transcripts."""

    name: ArmName = "A3"

    def phases(self, budget: ConsolidationBudget) -> list[PhaseSpec]:
        return [_organizer_phase(budget)]


class VerifiedDreamingArm(_WikiArm):
    """A4: an analyzer verifies memory against the transcript, then the organizer tidies it."""

    name: ArmName = "A4"

    def phases(self, budget: ConsolidationBudget) -> list[PhaseSpec]:
        return [
            PhaseSpec(
                name="analyzer",
                prompt_files=[CORE_PROMPT, ANALYZER_PROMPT],
                max_turns=budget.analyzer_max_turns,
                transcripts_readable=True,
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
