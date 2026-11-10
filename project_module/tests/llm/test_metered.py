import sqlite3
from pathlib import Path

import httpx2
import openai
import pytest
from fakes import FakeTransport, make_tag, text_reply
from project_module.config import ModelSpec
from project_module.contracts import Usage
from project_module.llm.ledger import BudgetExceeded, CostLedger
from project_module.llm.metered import MeteredClient
from pytest_mock import MockerFixture

MODELS = {
    "luna": ModelSpec(
        deployment="gpt-6-luna-deploy",
        input_usd_per_m=0.10,
        cached_input_usd_per_m=0.01,
        output_usd_per_m=0.50,
        kind="chat",
    ),
    "embed": ModelSpec(
        deployment="embed-deploy",
        input_usd_per_m=0.13,
        cached_input_usd_per_m=0.13,
        output_usd_per_m=0.0,
        kind="embedding",
    ),
}
MESSAGES = [{"role": "user", "content": "hello"}]
_REQUEST = httpx2.Request("POST", "https://example.invalid/openai/v1/chat/completions")


def _status_error[E: openai.APIStatusError](cls: type[E], status: int) -> E:
    return cls("boom", response=httpx2.Response(status, request=_REQUEST), body=None)


def _client(
    tmp_path: Path, transport: FakeTransport, *, phase_cap_usd: float = 10.0
) -> tuple[MeteredClient, CostLedger, list[float]]:
    ledger = CostLedger(
        tmp_path / "ledger.sqlite", phase="test", phase_cap_usd=phase_cap_usd, hard_cap_usd=200.0
    )
    sleeps: list[float] = []
    return MeteredClient(transport, ledger, MODELS, sleep=sleeps.append), ledger, sleeps


def _states(tmp_path: Path) -> list[str]:
    conn = sqlite3.connect(tmp_path / "ledger.sqlite")
    try:
        rows = conn.execute("SELECT state FROM reservations ORDER BY created_at, rowid").fetchall()
    finally:
        conn.close()
    return [state for (state,) in rows]


def _chat(client: MeteredClient, *, max_completion_tokens: int = 100) -> str | None:
    return client.chat(
        model="luna",
        messages=MESSAGES,
        tools=None,
        reasoning_effort="low",
        max_completion_tokens=max_completion_tokens,
        tag=make_tag(),
    ).content


def test_chat_settles_usage_and_sends_deployment_name(tmp_path: Path) -> None:
    transport = FakeTransport([text_reply("hi", input_tokens=1_000, output_tokens=200)])
    client, ledger, _ = _client(tmp_path, transport)

    content = _chat(client)

    assert content == "hi"
    assert transport.calls[0]["model"] == "gpt-6-luna-deploy"
    assert _states(tmp_path) == ["settled"]
    assert ledger.spent_usd() == pytest.approx(1_000 * 0.10e-6 + 200 * 0.50e-6)


def test_chat_passes_tool_choice_to_the_transport(tmp_path: Path) -> None:
    transport = FakeTransport([text_reply("hi")])
    client, _, _ = _client(tmp_path, transport)
    choice = {"type": "function", "name": "submit"}

    client.chat(
        model="luna",
        messages=MESSAGES,
        tools=None,
        reasoning_effort="low",
        max_completion_tokens=100,
        tag=make_tag(),
        tool_choice=choice,
    )

    assert transport.calls[0]["tool_choice"] == choice


def test_chat_over_budget_is_not_sent(tmp_path: Path) -> None:
    transport = FakeTransport([text_reply("hi")])
    client, _, _ = _client(tmp_path, transport, phase_cap_usd=0.001)

    with pytest.raises(BudgetExceeded):
        _chat(client, max_completion_tokens=10_000)

    assert transport.calls == []
    assert _states(tmp_path) == []


def test_metered_retries_rate_limit_with_same_reservation(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    transport = FakeTransport()
    limited = _status_error(openai.RateLimitError, 429)
    mocker.patch.object(transport, "chat", side_effect=[limited, limited, text_reply("ok")])
    client, _, sleeps = _client(tmp_path, transport)

    content = _chat(client)

    assert content == "ok"
    assert _states(tmp_path) == ["settled"]
    assert len(sleeps) == 2


def test_metered_releases_after_rate_limit_retries_run_out(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    transport = FakeTransport()
    mocker.patch.object(transport, "chat", side_effect=_status_error(openai.RateLimitError, 429))
    client, _, sleeps = _client(tmp_path, transport)

    with pytest.raises(openai.RateLimitError):
        _chat(client)

    assert _states(tmp_path) == ["released"]
    assert len(sleeps) == 6
    assert max(sleeps) <= 60


def test_metered_forfeits_on_connection_error_and_rereserves(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    transport = FakeTransport()
    errors = [
        openai.APIConnectionError(request=_REQUEST),
        _status_error(openai.InternalServerError, 503),
    ]
    mocker.patch.object(transport, "chat", side_effect=[*errors, text_reply("ok")])
    client, _, _ = _client(tmp_path, transport)

    content = _chat(client)

    assert content == "ok"
    assert _states(tmp_path) == ["forfeited", "forfeited", "settled"]


def test_metered_gives_up_after_two_uncertain_retries(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    transport = FakeTransport()
    mocker.patch.object(transport, "chat", side_effect=openai.APITimeoutError(request=_REQUEST))
    client, _, _ = _client(tmp_path, transport)

    with pytest.raises(openai.APITimeoutError):
        _chat(client)

    assert _states(tmp_path) == ["forfeited", "forfeited", "forfeited"]


def test_metered_releases_on_bad_request(tmp_path: Path, mocker: MockerFixture) -> None:
    transport = FakeTransport()
    mocker.patch.object(transport, "chat", side_effect=_status_error(openai.BadRequestError, 400))
    client, ledger, _ = _client(tmp_path, transport)

    with pytest.raises(openai.BadRequestError):
        _chat(client)

    assert _states(tmp_path) == ["released"]
    assert ledger.spent_usd() == 0.0


def test_metered_forfeits_on_unexpected_error(tmp_path: Path, mocker: MockerFixture) -> None:
    transport = FakeTransport()
    mocker.patch.object(transport, "chat", side_effect=RuntimeError("malformed response"))
    client, _, _ = _client(tmp_path, transport)

    with pytest.raises(RuntimeError, match="malformed response"):
        _chat(client)

    assert _states(tmp_path) == ["forfeited"]


def test_embed_settles_input_tokens(tmp_path: Path) -> None:
    transport = FakeTransport()
    client, ledger, _ = _client(tmp_path, transport)

    vectors = client.embed(model="embed", texts=["a", "b"], tag=make_tag(component="embedding"))

    assert len(vectors) == 2
    assert transport.embed_calls == [["a", "b"]]
    # FakeTransport bills 3 input tokens per text.
    assert ledger.spent_usd() == pytest.approx(6 * 0.13e-6)


def test_embed_of_nothing_makes_no_call(tmp_path: Path) -> None:
    transport = FakeTransport()
    client, _, _ = _client(tmp_path, transport)

    vectors = client.embed(model="embed", texts=[], tag=make_tag())

    assert vectors == []
    assert transport.embed_calls == []


def test_unknown_model_is_rejected(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, FakeTransport())

    with pytest.raises(ValueError, match="unknown model 'nope'"):
        client.embed(model="nope", texts=["a"], tag=make_tag())


def test_model_kind_must_match_call(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, FakeTransport())

    with pytest.raises(ValueError, match="model 'luna' is a chat model"):
        client.embed(model="luna", texts=["a"], tag=make_tag())


def test_usage_returned_by_transport_is_what_gets_billed(tmp_path: Path) -> None:
    reply = text_reply("hi")
    reply.usage = Usage(input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0)
    client, ledger, _ = _client(tmp_path, FakeTransport([reply]))

    _chat(client)

    assert ledger.spent_usd() == pytest.approx(0.01)


def test_metered_retries_content_filter_once_then_raises(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    from project_module.llm import ContentFiltered

    transport = FakeTransport()
    error = openai.BadRequestError(
        "filtered by the content management policy",
        response=httpx2.Response(400, request=_REQUEST),
        body={"code": "content_filter"},
    )
    mocker.patch.object(transport, "chat", side_effect=error)
    ledger = CostLedger(tmp_path / "l.sqlite", phase="test", phase_cap_usd=1.0, hard_cap_usd=10.0)
    client = MeteredClient(transport, ledger, MODELS, sleep=lambda _: None)
    with pytest.raises(ContentFiltered):
        client.chat(
            model="luna",
            messages=[{"role": "user", "content": "x"}],
            tools=None,
            reasoning_effort="low",
            max_completion_tokens=10,
            tag=make_tag(),
        )
    assert transport.chat.call_count == 2  # type: ignore[attr-defined]
    assert ledger.spent_usd() == 0.0
