"""What every arm receives from the runner, and the strategy interface arms implement."""

from dataclasses import dataclass
from typing import Literal, Protocol

from project_module.config import ArmName, ExperimentConfig
from project_module.contracts import ConsolidationRecord, CostTag, LLMClient, TaskEnv, ToolSet
from project_module.layout import RunLayout
from project_module.memory.store import MemoryStore
from project_module.retrieval import HybridIndex
from project_module.schemadrift.models import InjectedNote, Stream

Component = Literal["online", "consolidation", "probe", "embedding", "test"]


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

    def inject_false_note(self, ctx: ArmContext, note: InjectedNote) -> None: ...

    def after_session(self, ctx: ArmContext, session: int) -> ConsolidationRecord | None: ...
