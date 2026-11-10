"""Shared types and protocols. No logic lives here."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from project_module.config import ArmName, Phase


class Usage(BaseModel):
    input_tokens: int
    cached_input_tokens: int = 0
    output_tokens: int = 0


class CostTag(BaseModel):
    phase: Phase
    run_id: str
    stream_id: str
    arm: str
    session: int
    component: Literal["online", "consolidation", "probe", "embedding", "test"]


class ToolCall(BaseModel):
    id: str
    name: str
    # Arguments that are not valid JSON arrive as {"__invalid__": "<raw>"}.
    arguments: dict[str, Any]


class ChatResult(BaseModel):
    content: str | None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage
    finish_reason: str = "stop"
    # Appended verbatim to the next turn's messages.
    assistant_message: dict[str, Any]


# "auto", "required" or "none", or {"type": "function", "name": <tool>} to force one tool.
# None leaves the provider's default (auto).
ToolChoice = str | dict[str, Any]


class ChatTransport(Protocol):
    """Raw model access that knows nothing about cost."""

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str,
        max_completion_tokens: int,
        tool_choice: ToolChoice | None = None,
    ) -> ChatResult: ...

    def embed(self, *, model: str, texts: list[str]) -> tuple[list[list[float]], Usage]: ...


class LLMClient(Protocol):
    """Metered model access. Everything above the transport uses this."""

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str,
        max_completion_tokens: int,
        tag: CostTag,
        tool_choice: ToolChoice | None = None,
    ) -> ChatResult: ...

    def embed(self, *, model: str, texts: list[str], tag: CostTag) -> list[list[float]]: ...


class ToolSet(Protocol):
    def schemas(self) -> list[dict[str, Any]]: ...

    def dispatch(self, call: ToolCall) -> str:
        """Run one call. Failures come back as "ERROR: ..." text, never as exceptions."""
        ...


GradeCategory = Literal["correct", "stale", "sql_error", "wrong_result", "no_submission"]


class Grade(BaseModel):
    category: GradeCategory
    score: float

    @property
    def correct(self) -> bool:
        return self.category == "correct"


class TaskQuestion(BaseModel):
    qid: str
    session: int
    text: str
    drift_dependent: bool
    injected_target: bool


class TaskEnv(Protocol):
    """Everything that differs between SchemaDrift-SQL and CL-Bench."""

    env_id: str
    sessions: int
    max_queries: int
    submit_param: str

    def questions_for(self, session: int) -> list[TaskQuestion]: ...

    def run_query(self, session: int, sql: str) -> str: ...

    def submit_tool(self) -> dict[str, Any]: ...

    def grade(self, q: TaskQuestion, submission: str | None, queries_used: int) -> Grade: ...

    def feedback(self, q: TaskQuestion, grade: Grade) -> str | None: ...


class QuestionResult(BaseModel):
    stream_id: str
    arm: ArmName
    session: int
    qid: str
    category: GradeCategory
    score: float
    queries: int
    ok_queries: int
    tool_calls: int
    tool_errors: int
    turns: int
    drift_dependent: bool
    injected_target: bool
    # Markers of the false notes present in memory (marker or claim) when the question started.
    active_markers: list[str] = Field(default_factory=list)

    @property
    def correct(self) -> bool:
        return self.category == "correct"


class ProbeResult(BaseModel):
    stream_id: str
    arm: ArmName
    session: int
    fact_id: str
    injected: bool
    correct: bool


class ConsolidationRecord(BaseModel):
    stream_id: str
    arm: ArmName
    session: int
    phases: list[str]
    turns: int
    input_tokens: int
    output_tokens: int
    stopped: str
    lint_problems: list[str] = Field(default_factory=list)
    # Markers of the false notes still present in memory after this run (marker or claim).
    injected_markers_present: list[str] = Field(default_factory=list)
    # Whether this run left `injected.md` with different content than it found.
    injected_updated: bool


RunStatus = Literal["running", "completed", "budget_exhausted", "interrupted", "failed"]
