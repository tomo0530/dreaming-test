"""`ChatTransport` for the OpenAI-compatible v1 endpoint of an Azure AI Foundry resource."""

import json
import os
from typing import Any, Final, Self, cast
from urllib.parse import urlsplit

from openai import OpenAI, omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)
from openai.types.shared import ReasoningEffort

from project_module.contracts import ChatResult, ToolCall, Usage
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


def _to_chat_result(completion: ChatCompletion) -> ChatResult:
    if not completion.choices:
        raise RuntimeError("chat completion has no choices")
    if completion.usage is None:
        raise RuntimeError("chat completion has no usage")
    choice = completion.choices[0]
    message = choice.message
    tool_calls: list[ToolCall] = []
    raw_calls: list[dict[str, Any]] = []
    for call in message.tool_calls or []:
        if call.type != "function":
            raise RuntimeError(f"unsupported tool call type {call.type!r}")
        tool_calls.append(
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=_parse_arguments(call.function.arguments),
            )
        )
        raw_calls.append(
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
        )
    # Rebuilt by hand so only fields the API accepts on input are sent back next turn.
    assistant_message: dict[str, Any] = {"role": "assistant", "content": message.content}
    if raw_calls:
        assistant_message["tool_calls"] = raw_calls
    details = completion.usage.prompt_tokens_details
    cached = details.cached_tokens if details is not None and details.cached_tokens else 0
    return ChatResult(
        content=message.content,
        tool_calls=tool_calls,
        usage=Usage(
            input_tokens=completion.usage.prompt_tokens,
            cached_input_tokens=cached,
            output_tokens=completion.usage.completion_tokens,
        ),
        finish_reason=choice.finish_reason,
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
    ) -> ChatResult:
        # `temperature` is never sent: reasoning deployments reject it.
        completion = self._client.chat.completions.create(
            model=model,
            messages=cast(list[ChatCompletionMessageParam], messages),
            tools=cast(list[ChatCompletionToolUnionParam], tools) if tools else omit,
            reasoning_effort=cast(ReasoningEffort, reasoning_effort),
            max_completion_tokens=max_completion_tokens,
        )
        return _to_chat_result(completion)

    def embed(self, *, model: str, texts: list[str]) -> tuple[list[list[float]], Usage]:
        response = self._client.embeddings.create(model=model, input=texts)
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered], Usage(
            input_tokens=response.usage.prompt_tokens
        )
