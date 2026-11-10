"""Metered model access: cost ledger, retrying client, Foundry transport and tool loop."""

from typing import Final

# `ToolCall.arguments` holds {INVALID_ARGUMENTS_KEY: raw} when the model's arguments are not a
# JSON object (see `project_module.contracts.ToolCall`).
INVALID_ARGUMENTS_KEY: Final = "__invalid__"


class ContentFiltered(RuntimeError):
    """The provider's content filter rejected a request (not billed; the run continues)."""
