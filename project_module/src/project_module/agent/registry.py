"""The single place that maps arm names to arm classes and to the state each arm keeps."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from project_module.agent.context import Arm
from project_module.agent.memory_arms import OrganizeOnlyArm, RollingSummaryArm, VerifiedDreamingArm
from project_module.agent.online_arms import OracleArm, RawRagArm, StatelessArm
from project_module.config import ArmName


@dataclass(frozen=True)
class ArmSpec:
    """How to build an arm and which per-job state the runner must give it."""

    factory: Callable[[], Arm]
    # A memory directory (`ArmContext.memory`), snapshotted after every session.
    keeps_memory: bool
    # A transcript index (`ArmContext.index`), snapshotted after every session.
    uses_index: bool


ARM_SPECS: Final[Mapping[ArmName, ArmSpec]] = {
    "A0": ArmSpec(StatelessArm, keeps_memory=False, uses_index=False),
    "A1": ArmSpec(RollingSummaryArm, keeps_memory=True, uses_index=False),
    "A2": ArmSpec(RawRagArm, keeps_memory=False, uses_index=True),
    "A3": ArmSpec(OrganizeOnlyArm, keeps_memory=True, uses_index=False),
    "A4": ArmSpec(VerifiedDreamingArm, keeps_memory=True, uses_index=False),
    "A5": ArmSpec(OracleArm, keeps_memory=True, uses_index=False),
}


def arm_spec(name: ArmName) -> ArmSpec:
    return ARM_SPECS[name]


def make_arm(name: ArmName) -> Arm:
    """A fresh instance of the arm called `name`."""
    return arm_spec(name).factory()
