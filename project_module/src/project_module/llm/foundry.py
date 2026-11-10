"""`ChatTransport` for the OpenAI-compatible v1 endpoint of an Azure AI Foundry resource.

Chat calls go through the Responses API: gpt-6-luna rejects function tools combined with
`reasoning_effort` on /chat/completions. The rest of the harness keeps chat-style messages;
this module translates them to Responses input items and back.
"""

import json
import os
from typing import Any, Final, Self, cast
from urllib.parse import urlsplit

from openai import OpenAI, omit
from openai.types.responses import Response

from project_module.contracts import ChatResult, ToolCall, ToolChoice, Usage
from project_module.llm import INVALID_ARGUMENTS_KEY

ENDPOINT_ENV: Final = "AZURE_OPENAI_ENDPOINT"
API_KEY_ENV: Final = "AZURE_OPENAI_API_KEY"


def foundry_base_url(endpoint: str) -> str:
    """Map a Foundry project URL (`https://<host>/api/projects/<p>`) to `https://<host>/openai/v1/`.

    The endpoint itself never appears in error messages: its host name is treated as secret.
    """
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"{ENDPOINT_ENV} must be an https URL with a host")
    return f"https://{parts.netloc}/openai/v1/"


def _parse_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {INVALID_ARGUMENTS_KEY: raw}
    if not isinstance(parsed, dict):
        return {INVALID_ARGUMENTS_KEY: raw}
    return parsed


def _to_response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten chat-style `{"type": "function", "function": {...}}` schemas."""
    flat: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function", tool)
        item: dict[str, Any] = {"type": "function", "name": function["name"]}
        if "description" in function:
            item["description"] = function["description"]
        item["parameters"] = function.get("parameters", {"type": "object", "properties": {}})
        flat.append(item)
    return flat


def _to_response_tool_choice(choice: ToolChoice) -> str | dict[str, Any]:
    """Pass modes through; flatten a chat-style `{"type": "function", "function": {"name"}}`."""
    if isinstance(choice, str):
        return choice
    function = choice.get("function")
    if isinstance(function, dict):
        return {"type": "function", "name": function["name"]}
    return dict(choice)


def _to_response_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate chat-style messages (incl. assistant tool calls and tool results)."""
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        content = message.get("content")
        if content:
            items.append({"role": role, "content": content})
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call["function"]
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": function["name"],
                        "arguments": function["arguments"],
                    }
                )
    return items


def _to_chat_result(response: Response) -> ChatResult:
    if response.usage is None:
        raise RuntimeError("response has no usage")
    tool_calls: list[ToolCall] = []
    raw_calls: list[dict[str, Any]] = []
    for item in response.output:
        if item.type != "function_call":
            continue
        tool_calls.append(
            ToolCall(id=item.call_id, name=item.name, arguments=_parse_arguments(item.arguments))
        )
        raw_calls.append(
            {
                "id": item.call_id,
                "type": "function",
                "function": {"name": item.name, "arguments": item.arguments},
            }
        )
    text = response.output_text or None
    assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
    if raw_calls:
        assistant_message["tool_calls"] = raw_calls
    incomplete = response.incomplete_details
    if response.status == "incomplete" and incomplete is not None:
        finish_reason = (
            "length" if incomplete.reason == "max_output_tokens" else str(incomplete.reason)
        )
    else:
        finish_reason = "tool_calls" if tool_calls else "stop"
    details = response.usage.input_tokens_details
    cached = details.cached_tokens if details is not None and details.cached_tokens else 0
    return ChatResult(
        content=text,
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=response.usage.input_tokens,
            cached_input_tokens=cached,
            output_tokens=response.usage.output_tokens,
        ),
        finish_reason=finish_reason,
        assistant_message=assistant_message,
    )


class FoundryTransport:
    """Unmetered chat and embedding calls; retries are left to `MeteredClient`."""

    def __init__(self, base_url: str, api_key: str, timeout_s: float = 120.0) -> None:
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)

    @classmethod
    def from_env(cls) -> Self:
        """Build from `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY`."""
        endpoint = os.environ.get(ENDPOINT_ENV)
        if not endpoint:
            raise RuntimeError(f"{ENDPOINT_ENV} is not set")
        api_key = os.environ.get(API_KEY_ENV)
        if not api_key:
            raise RuntimeError(f"{API_KEY_ENV} is not set")
        return cls(foundry_base_url(endpoint), api_key)

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str,
        max_completion_tokens: int,
        tool_choice: ToolChoice | None = None,
    ) -> ChatResult:
        # `temperature` is never sent: reasoning deployments reject it. Reasoning items are
        # not carried between turns (store=False, stateless calls).
        response = self._client.responses.create(
            model=model,
            input=cast(Any, _to_response_input(messages)),
            tools=cast(Any, _to_response_tools(tools)) if tools else omit,
            tool_choice=(
                cast(Any, _to_response_tool_choice(tool_choice))
                if tool_choice is not None
                else omit
            ),
            reasoning=cast(Any, {"effort": reasoning_effort}),
            max_output_tokens=max_completion_tokens,
            store=False,
        )
        return _to_chat_result(response)

    def embed(self, *, model: str, texts: list[str]) -> tuple[list[list[float]], Usage]:
        response = self._client.embeddings.create(model=model, input=texts)
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered], Usage(
            input_tokens=response.usage.prompt_tokens
        )
