"""Test doubles shared by all test modules."""

import hashlib
from typing import Any

from project_module.contracts import ChatResult, CostTag, ToolCall, Usage


def text_reply(text: str, *, input_tokens: int = 10, output_tokens: int = 5) -> ChatResult:
    return ChatResult(
        content=text,
        tool_calls=[],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        assistant_message={"role": "assistant", "content": text},
    )


def tool_reply(
    *calls: tuple[str, dict[str, Any]], input_tokens: int = 10, output_tokens: int = 5
) -> ChatResult:
    tool_calls = [
        ToolCall(id=f"call_{i}", name=name, arguments=args) for i, (name, args) in enumerate(calls)
    ]
    return ChatResult(
        content=None,
        tool_calls=tool_calls,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": "{}"}}
                for c in tool_calls
            ],
        },
    )


def fake_embedding(text: str, dims: int = 8) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in digest[:dims]]


class FakeTransport:
    """Replays a script of ChatResults in order and records every call."""

    def __init__(self, script: list[ChatResult] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []
        self.embed_calls: list[list[str]] = []

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str,
        max_completion_tokens: int,
    ) -> ChatResult:
        self.calls.append(
            {
                "model": model,
                "messages": [dict(m) for m in messages],
                "tools": tools,
                "reasoning_effort": reasoning_effort,
                "max_completion_tokens": max_completion_tokens,
            }
        )
        if not self.script:
            return text_reply("(script exhausted)")
        return self.script.pop(0)

    def embed(self, *, model: str, texts: list[str]) -> tuple[list[list[float]], Usage]:
        self.embed_calls.append(list(texts))
        return [fake_embedding(t) for t in texts], Usage(input_tokens=len(texts) * 3)


class FakeLLM:
    """LLMClient without a ledger, delegating to a FakeTransport."""

    def __init__(self, script: list[ChatResult] | None = None) -> None:
        self.transport = FakeTransport(script)
        self.tags: list[CostTag] = []

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.transport.calls

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        reasoning_effort: str,
        max_completion_tokens: int,
        tag: CostTag,
    ) -> ChatResult:
        self.tags.append(tag)
        return self.transport.chat(
            model=model,
            messages=messages,
            tools=tools,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
        )

    def embed(self, *, model: str, texts: list[str], tag: CostTag) -> list[list[float]]:
        self.tags.append(tag)
        vectors, _ = self.transport.embed(model=model, texts=texts)
        return vectors


def make_tag(**overrides: Any) -> CostTag:
    base: dict[str, Any] = {
        "phase": "test",
        "run_id": "run-test",
        "stream_id": "s-test",
        "arm": "A0",
        "session": 1,
        "component": "test",
    }
    base.update(overrides)
    return CostTag(**base)
