"""`LLMClient` that reserves every call in the cost ledger before sending it."""

import logging
import time
from collections.abc import Callable
from functools import partial
from typing import Any, Final, Literal

import openai
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from project_module.config import ModelSpec
from project_module.contracts import ChatResult, ChatTransport, CostTag, Usage
from project_module.llm import ContentFiltered
from project_module.llm.ledger import CostLedger
from project_module.tokens import count_chat_tokens, count_tokens

logger = logging.getLogger(__name__)

RATE_LIMIT_RETRIES: Final = 6
UNCERTAIN_RETRIES: Final = 2
BACKOFF_MIN_S: Final = 1.0
BACKOFF_MAX_S: Final = 60.0

# The request may have reached the model and been billed, so its reservation is forfeited
# and every retry reserves anew.
_UNCERTAIN_ERRORS: Final = (openai.APIConnectionError, openai.InternalServerError)


CONTENT_FILTER_RETRIES: Final = 1


def _is_content_filter(exc: openai.APIStatusError) -> bool:
    """Azure reports filtered prompts as a 400 with code `content_filter`."""
    return getattr(exc, "code", None) == "content_filter" or "content management policy" in str(exc)


class MeteredClient:
    """Reserve, send, then settle; retry according to what each failure says about billing.

    - 429: not billed; retried under the same reservation up to `RATE_LIMIT_RETRIES` times.
    - Connection errors, timeouts and 5xx: billing unknown; the reservation is forfeited and
      the call is retried under a new reservation up to `UNCERTAIN_RETRIES` times.
    - Other 4xx (including content-filter 400s): not billed; released and raised.
    - Anything else (e.g. a malformed response): billing unknown; forfeited and raised.

    Parameters
    ----------
    transport
        Unmetered model access.
    ledger
        Ledger shared with every other process of the experiment.
    models
        Model specs keyed by the names callers use; `deployment` is what the transport sees.
    sleep
        Backoff sleep, injectable for tests.
    """

    def __init__(
        self,
        transport: ChatTransport,
        ledger: CostLedger,
        models: dict[str, ModelSpec],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._ledger = ledger
        self._models = models
        self._sleep = sleep

    def _spec(self, model: str, kind: Literal["chat", "embedding"]) -> ModelSpec:
        spec = self._models.get(model)
        if spec is None:
            raise ValueError(f"unknown model {model!r}")
        if spec.kind != kind:
            raise ValueError(f"model {model!r} is a {spec.kind} model; this call needs {kind}")
        return spec

    def _backoff(self) -> wait_exponential:
        return wait_exponential(multiplier=1, min=BACKOFF_MIN_S, max=BACKOFF_MAX_S)

    def _send_once[R](
        self,
        send: Callable[[], R],
        usage_of: Callable[[R], Usage],
        *,
        spec: ModelSpec,
        input_tokens: int,
        max_output_tokens: int,
        tag: CostTag,
    ) -> R:
        rid = self._ledger.reserve(
            spec, input_tokens=input_tokens, max_output_tokens=max_output_tokens, tag=tag
        )
        rate_limited = Retrying(
            retry=retry_if_exception_type(openai.RateLimitError),
            stop=stop_after_attempt(RATE_LIMIT_RETRIES + 1),
            wait=self._backoff(),
            sleep=self._sleep,
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        try:
            result = rate_limited(send)
        except _UNCERTAIN_ERRORS:
            self._ledger.forfeit(rid)
            raise
        except openai.APIStatusError as exc:
            self._ledger.release(rid)
            if _is_content_filter(exc):
                raise ContentFiltered(
                    f"request blocked by the content filter ({tag.component})"
                ) from exc
            raise
        except BaseException:
            self._ledger.forfeit(rid)
            raise
        self._ledger.settle(rid, usage_of(result))
        return result

    def _metered[R](
        self,
        send: Callable[[], R],
        usage_of: Callable[[R], Usage],
        *,
        spec: ModelSpec,
        input_tokens: int,
        max_output_tokens: int,
        tag: CostTag,
    ) -> R:
        uncertain = Retrying(
            retry=retry_if_exception_type(_UNCERTAIN_ERRORS),
            stop=stop_after_attempt(UNCERTAIN_RETRIES + 1),
            wait=self._backoff(),
            sleep=self._sleep,
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        attempt = partial(
            self._send_once,
            send,
            usage_of,
            spec=spec,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            tag=tag,
        )
        filtered = Retrying(
            retry=retry_if_exception_type(ContentFiltered),
            stop=stop_after_attempt(CONTENT_FILTER_RETRIES + 1),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )
        return filtered(lambda: uncertain(attempt))

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
        spec = self._spec(model, "chat")
        send = partial(
            self._transport.chat,
            model=spec.deployment,
            messages=messages,
            tools=tools,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
        )
        return self._metered(
            send,
            _chat_usage,
            spec=spec,
            input_tokens=count_chat_tokens(messages, tools),
            max_output_tokens=max_completion_tokens,
            tag=tag,
        )

    def embed(self, *, model: str, texts: list[str], tag: CostTag) -> list[list[float]]:
        spec = self._spec(model, "embedding")
        if not texts:
            return []
        send = partial(self._transport.embed, model=spec.deployment, texts=texts)
        vectors, _ = self._metered(
            send,
            _embedding_usage,
            spec=spec,
            input_tokens=sum(count_tokens(text) for text in texts),
            max_output_tokens=0,
            tag=tag,
        )
        return vectors


def _chat_usage(result: ChatResult) -> Usage:
    return result.usage


def _embedding_usage(result: tuple[list[list[float]], Usage]) -> Usage:
    return result[1]
