import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fakes import make_tag
from openai import omit
from openai.types import CreateEmbeddingResponse
from openai.types.chat import ChatCompletion
from project_module.config import load_models
from project_module.layout import ledger_path
from project_module.llm.foundry import FoundryTransport, foundry_base_url
from project_module.llm.ledger import CostLedger
from project_module.llm.metered import MeteredClient
from pytest_mock import MockerFixture

REPO_ROOT = Path(__file__).resolve().parents[3]


def _completion(message: dict[str, Any], *, finish_reason: str = "stop") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-6-luna",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 100},
            },
        }
    )


def _transport_with(mocker: MockerFixture, completion: Any) -> tuple[FoundryTransport, MagicMock]:
    openai_cls = mocker.patch("project_module.llm.foundry.OpenAI")
    sdk = openai_cls.return_value
    sdk.chat.completions.create.return_value = completion
    sdk.embeddings.create.return_value = completion
    return FoundryTransport("https://h.example/openai/v1/", "key"), openai_cls


def _chat(transport: FoundryTransport, tools: list[dict[str, Any]] | None = None) -> Any:
    return transport.chat(
        model="gpt-6-luna",
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        reasoning_effort="low",
        max_completion_tokens=64,
    )


def test_foundry_base_url_strips_project_path() -> None:
    assert (
        foundry_base_url("https://r.services.ai.azure.com/api/projects/p")
        == "https://r.services.ai.azure.com/openai/v1/"
    )


def test_foundry_base_url_rejects_endpoint_without_https_host() -> None:
    with pytest.raises(ValueError, match="must be an https URL"):
        foundry_base_url("r.services.ai.azure.com/api/projects/p")


def test_foundry_transport_disables_sdk_retries(mocker: MockerFixture) -> None:
    _, openai_cls = _transport_with(mocker, _completion({"role": "assistant", "content": "x"}))

    kwargs = openai_cls.call_args.kwargs
    assert (kwargs["max_retries"], kwargs["base_url"], kwargs["timeout"]) == (
        0,
        "https://h.example/openai/v1/",
        120.0,
    )


def test_foundry_transport_omits_temperature(mocker: MockerFixture) -> None:
    transport, openai_cls = _transport_with(
        mocker, _completion({"role": "assistant", "content": "x"})
    )

    _chat(transport)

    kwargs = openai_cls.return_value.chat.completions.create.call_args.kwargs
    assert "temperature" not in kwargs
    assert kwargs.get("tools", omit) is omit
    assert (kwargs["max_completion_tokens"], kwargs["reasoning_effort"], kwargs["model"]) == (
        64,
        "low",
        "gpt-6-luna",
    )


def test_foundry_transport_maps_usage_with_cached_tokens(mocker: MockerFixture) -> None:
    transport, _ = _transport_with(mocker, _completion({"role": "assistant", "content": "answer"}))

    result = _chat(transport)

    assert (result.content, result.finish_reason) == ("answer", "stop")
    assert (
        result.usage.input_tokens,
        result.usage.cached_input_tokens,
        result.usage.output_tokens,
    ) == (120, 100, 30)
    assert result.assistant_message == {"role": "assistant", "content": "answer"}


def test_foundry_transport_parses_tool_calls_and_keeps_raw_arguments(
    mocker: MockerFixture,
) -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "query", "arguments": '{"sql": "SELECT 1"}'},
            },
            {"id": "c2", "type": "function", "function": {"name": "query", "arguments": "{bad"}},
            {"id": "c3", "type": "function", "function": {"name": "query", "arguments": "[1]"}},
        ],
    }
    transport, openai_cls = _transport_with(
        mocker, _completion(message, finish_reason="tool_calls")
    )
    tools = [{"type": "function", "function": {"name": "query", "parameters": {}}}]

    result = _chat(transport, tools=tools)

    assert [c.arguments for c in result.tool_calls] == [
        {"sql": "SELECT 1"},
        {"__invalid__": "{bad"},
        {"__invalid__": "[1]"},
    ]
    assert result.assistant_message["tool_calls"][1] == {
        "id": "c2",
        "type": "function",
        "function": {"name": "query", "arguments": "{bad"},
    }
    assert openai_cls.return_value.chat.completions.create.call_args.kwargs["tools"] == tools


def test_foundry_transport_embeds_in_index_order(mocker: MockerFixture) -> None:
    response = CreateEmbeddingResponse.model_validate(
        {
            "object": "list",
            "model": "text-embedding-3-large",
            "data": [
                {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
            ],
            "usage": {"prompt_tokens": 7, "total_tokens": 7},
        }
    )
    transport, _ = _transport_with(mocker, response)

    vectors, usage = transport.embed(model="text-embedding-3-large", texts=["a", "b"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert usage.input_tokens == 7


def test_from_env_requires_both_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://h.example/api/projects/p")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="AZURE_OPENAI_API_KEY is not set"):
        FoundryTransport.from_env()


def test_from_env_derives_base_url(mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    openai_cls = mocker.patch("project_module.llm.foundry.OpenAI")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://h.example/api/projects/p")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "secret")

    FoundryTransport.from_env()

    kwargs = openai_cls.call_args.kwargs
    assert (kwargs["base_url"], kwargs["api_key"]) == ("https://h.example/openai/v1/", "secret")


@pytest.mark.integrate
@pytest.mark.skipif(
    not (os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY")),
    reason="needs AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY (uv run --env-file .env)",
)
def test_foundry_luna_roundtrip() -> None:
    models = load_models(REPO_ROOT / "configs" / "models.yaml")
    ledger = CostLedger(
        ledger_path(REPO_ROOT / "outputs"), phase="test", phase_cap_usd=1.0, hard_cap_usd=200.0
    )
    client = MeteredClient(FoundryTransport.from_env(), ledger, models)
    spent_before = ledger.spent_usd(phase="test")

    result = client.chat(
        model="gpt-6-luna",
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        tools=None,
        reasoning_effort="low",
        max_completion_tokens=16,
        tag=make_tag(run_id="integrate-roundtrip"),
    )

    assert result.usage.input_tokens > 0
    assert 0 < ledger.spent_usd(phase="test") - spent_before < 0.01
