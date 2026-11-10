"""Experiment and model configuration loaded from `configs/`."""

from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ArmName = Literal["A0", "A1", "A2", "A3", "A4", "A5"]
Phase = Literal["test", "dev", "pilot", "pilot_inj", "pilot_rev", "main", "clbench"]


# Money must be a finite number: NaN compares false with every cap and would disable it.
PriceUsd = Annotated[float, Field(ge=0, allow_inf_nan=False)]
CapUsd = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelSpec(_Frozen):
    deployment: str
    input_usd_per_m: PriceUsd
    cached_input_usd_per_m: PriceUsd
    output_usd_per_m: PriceUsd
    kind: Literal["chat", "embedding"]


class OnlineBudget(_Frozen):
    max_turns: int = 8
    max_queries: int = 8
    max_completion_tokens: int = 2048
    memory_read_tokens: int = 6000
    sql_row_limit: int = 50
    sql_timeout_s: float = 5.0


class ConsolidationBudget(_Frozen):
    analyzer_max_turns: int = 10
    organizer_max_turns: int = 12
    max_completion_tokens: int = 8192
    max_input_tokens_per_run: int = 900_000


class Budgets(_Frozen):
    online: OnlineBudget = Field(default_factory=OnlineBudget)
    consolidation: ConsolidationBudget = Field(default_factory=ConsolidationBudget)
    injected_tokens: int = 2000
    max_input_tokens_per_call: int = 64_000
    phase_cap_usd: CapUsd
    hard_cap_usd: CapUsd = 200.0


class ExperimentConfig(_Frozen):
    name: str
    phase: Phase
    task: Literal["schemadrift", "clbench"] = "schemadrift"
    arms: list[ArmName]
    arm_overrides: dict[str, list[ArmName]] = Field(default_factory=dict)
    stream_ids: list[str]
    seeds: list[int]
    sessions: int = 12
    questions_per_session: int = 6
    inject_false_notes: bool
    agent_model: str
    consolidator_model: str
    embedding_model: str
    agent_reasoning_effort: str = "low"
    consolidator_reasoning_effort: str = "medium"
    probe_sessions: list[int] = Field(default_factory=lambda: [4, 8, 12])
    probes_per_checkpoint: int = 8
    concurrency: int = 4
    budgets: Budgets

    @model_validator(mode="after")
    def _check_streams(self) -> Self:
        if len(self.stream_ids) != len(self.seeds):
            raise ValueError("stream_ids and seeds must have the same length")
        unknown = set(self.arm_overrides) - set(self.stream_ids)
        if unknown:
            raise ValueError(f"arm_overrides refer to unknown streams: {sorted(unknown)}")
        return self

    def arms_for(self, stream_id: str) -> list[ArmName]:
        return self.arm_overrides.get(stream_id, self.arms)


def load_experiment(path: Path) -> ExperimentConfig:
    with path.open(encoding="utf-8") as f:
        return ExperimentConfig.model_validate(yaml.safe_load(f))


def load_models(path: Path) -> dict[str, ModelSpec]:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return {name: ModelSpec.model_validate(spec) for name, spec in raw["models"].items()}
