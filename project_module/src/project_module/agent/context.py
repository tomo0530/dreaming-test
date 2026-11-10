"""What every arm receives from the runner, and the strategy interface arms implement."""

import threading
from dataclasses import dataclass, field
from typing import Literal, Protocol

from project_module.config import ArmName, ExperimentConfig
from project_module.contracts import ConsolidationRecord, CostTag, LLMClient, TaskEnv, ToolSet
from project_module.layout import RunLayout
from project_module.memory.store import MemoryStore
from project_module.retrieval import HybridIndex
from project_module.schemadrift.models import InjectedNote, Stream

Component = Literal["online", "consolidation", "probe", "embedding", "test"]
StopReason = Literal["budget_exhausted", "interrupted"]


class StopRequested(Exception):
    """A job saw the run's stop signal between two questions and gave up its session."""


class StopSignal:
    """Asks every job of a run to stop, and says why; the first reason given wins."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: StopReason = "budget_exhausted"

    def request(self, reason: StopReason) -> None:
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> StopReason:
        return self._reason


@dataclass
class ArmContext:
    env: TaskEnv
    # Only SchemaDrift streams have one; CL-Bench runs pass None.
    stream: Stream | None
    config: ExperimentConfig
    client: LLMClient
    layout: RunLayout
    run_id: str
    stream_id: str
    # Only arms that keep memory get a store.
    memory: MemoryStore | None
    # Read-only view of this job's transcripts directory.
    transcripts: MemoryStore
    # Only A2 gets an index.
    index: HybridIndex | None
    # Shared by every job of the run; sessions check it before each question.
    stop: StopSignal = field(default_factory=StopSignal)

    def tag(self, arm: ArmName, session: int, component: Component) -> CostTag:
        return CostTag(
            phase=self.config.phase,
            run_id=self.run_id,
            stream_id=self.stream_id,
            arm=arm,
            session=session,
            component=component,
        )


class Arm(Protocol):
    name: ArmName

    def setup(self, ctx: ArmContext) -> None: ...

    def injected_context(self, ctx: ArmContext, session: int) -> str: ...

    def online_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        """Called once per question; must return a fresh instance."""
        ...

    def probe_tools(self, ctx: ArmContext, session: int) -> ToolSet | None:
        """Read-only tools for probes."""
        ...

    def inject_false_note(
        self, ctx: ArmContext, note: InjectedNote, *, expose: bool = False
    ) -> None:
        """Write a false note into the arm's memory; `expose` also appends it to `injected.md`."""
        ...

    def after_session(self, ctx: ArmContext, session: int) -> ConsolidationRecord | None: ...
