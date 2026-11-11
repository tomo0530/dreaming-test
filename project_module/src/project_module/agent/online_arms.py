"""Arms whose memory is not written by a consolidator: A0 Stateless, A2 Raw-RAG, A5 Oracle."""

import logging
from collections.abc import Sequence
from typing import Final, Literal

from project_module.agent.context import ArmContext
from project_module.config import ArmName
from project_module.contracts import ConsolidationRecord, TaskQuestion, ToolSet
from project_module.memory.store import MemoryStore
from project_module.memory.tools import MemoryReadTools
from project_module.retrieval import HybridIndex, TranscriptSearchTools
from project_module.schemadrift.models import Fact, InjectedNote, Stream
from project_module.tokens import count_tokens

logger = logging.getLogger(__name__)

INJECTED_FILE: Final = "injected.md"
FACTS_FILE: Final = "facts.md"
_ORACLE_HEADER: Final = "# Rules of the database that hold in this session"
_ORACLE_OVERFLOW: Final = f"More rules are listed in `{FACTS_FILE}` (read it with memory_read)."
_FACTS_HEADER: Final = "# Further rules of the database that hold in this session"
_NO_FURTHER_FACTS: Final = f"(none: every rule is listed in `{INJECTED_FILE}`)"


class StatelessArm:
    """A0: no memory, no tools beyond the database, nothing injected."""

    name: ArmName = "A0"

    def setup(self, ctx: ArmContext) -> None:
        return None

    def injected_context(self, ctx: ArmContext, session: int) -> str:
        return ""

    def online_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        return None

    def question_context(self, ctx: ArmContext, question: TaskQuestion) -> str:
        return ""

    def probe_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        return None

    def inject_false_note(
        self, ctx: ArmContext, note: InjectedNote, *, expose: bool = False
    ) -> None:
        return None

    def after_session(self, ctx: ArmContext, session: int) -> ConsolidationRecord | None:
        return None


def _require_index(ctx: ArmContext) -> HybridIndex:
    if ctx.index is None:
        raise ValueError("RawRagArm needs ArmContext.index")
    return ctx.index


class RawRagArm:
    """A2: every finished transcript is indexed; the agent searches them with `search_transcripts`."""

    name: ArmName = "A2"

    def setup(self, ctx: ArmContext) -> None:
        _require_index(ctx)

    def injected_context(self, ctx: ArmContext, session: int) -> str:
        return ""

    def online_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        return self._search_tools(ctx, session, "online")

    def question_context(self, ctx: ArmContext, question: TaskQuestion) -> str:
        return ""

    def probe_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        return self._search_tools(ctx, session, "probe")

    def inject_false_note(
        self, ctx: ArmContext, note: InjectedNote, *, expose: bool = False
    ) -> None:
        return None

    def after_session(self, ctx: ArmContext, session: int) -> ConsolidationRecord | None:
        transcript = ctx.layout.transcript_file(ctx.stream_id, self.name, session)
        chunks = _require_index(ctx).add_transcript(
            session,
            transcript.read_text(encoding="utf-8"),
            tag=ctx.tag(self.name, session, "embedding"),
        )
        logger.info(
            "%s %s: indexed session %d (%d chunks)", ctx.stream_id, self.name, session, chunks
        )
        return None

    def _search_tools(
        self, ctx: ArmContext, session: int, component: Literal["online", "probe"]
    ) -> ToolSet:
        return TranscriptSearchTools(
            _require_index(ctx),
            token_budget=ctx.config.budgets.online.memory_read_tokens,
            tag=ctx.tag(self.name, session, component),
        )


def _fact_line(fact: Fact) -> str:
    return f"- {fact.subject} ({fact.kind.replace('_', ' ')}): {fact.value}"


def _render_injected(lines: Sequence[str], *, overflow: bool) -> str:
    footer = [_ORACLE_OVERFLOW] if overflow else []
    return "\n".join([_ORACLE_HEADER, *lines, *footer]) + "\n"


def _split_for_injection(lines: Sequence[str], token_limit: int) -> int:
    """How many leading lines fit in the injected layer, overflow footer included."""
    for keep in range(len(lines), -1, -1):
        if count_tokens(_render_injected(lines[:keep], overflow=keep < len(lines))) <= token_limit:
            return keep
    raise ValueError(f"budgets.injected_tokens={token_limit} cannot hold the oracle header")


def _require_oracle_inputs(ctx: ArmContext) -> tuple[Stream, MemoryStore]:
    if ctx.stream is None or ctx.memory is None:
        raise ValueError("OracleArm needs ArmContext.stream and ArmContext.memory")
    return ctx.stream, ctx.memory


class OracleArm:
    """A5: the rules valid in the current session, generated from the gold fact ledger.

    One line per valid fact in `fact_id` order goes to `injected.md` while it stays within
    `budgets.injected_tokens`; the remaining lines go to `facts.md`, readable with the memory
    tools.
    """

    name: ArmName = "A5"

    def setup(self, ctx: ArmContext) -> None:
        _require_oracle_inputs(ctx)

    def injected_context(self, ctx: ArmContext, session: int) -> str:
        stream, memory = _require_oracle_inputs(ctx)
        lines = [_fact_line(fact) for fact in stream.valid_facts(session)]
        keep = _split_for_injection(lines, ctx.config.budgets.injected_tokens)
        injected = _render_injected(lines[:keep], overflow=keep < len(lines))
        rest = lines[keep:] or [_NO_FURTHER_FACTS]
        memory.write(INJECTED_FILE, injected)
        memory.write(FACTS_FILE, "\n".join([_FACTS_HEADER, *rest]) + "\n")
        return injected

    def online_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        return self._read_tools(ctx)

    def question_context(self, ctx: ArmContext, question: TaskQuestion) -> str:
        return ""

    def probe_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        return self._read_tools(ctx)

    def inject_false_note(
        self, ctx: ArmContext, note: InjectedNote, *, expose: bool = False
    ) -> None:
        return None

    def after_session(self, ctx: ArmContext, session: int) -> ConsolidationRecord | None:
        return None

    @staticmethod
    def _read_tools(ctx: ArmContext) -> ToolSet:
        _, memory = _require_oracle_inputs(ctx)
        return MemoryReadTools(
            memory,
            token_budget=ctx.config.budgets.online.memory_read_tokens,
            writable_prefix=None,
        )
