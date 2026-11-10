"""Token counting with an explicit encoding (tiktoken does not map gpt-6 names)."""

import json
from functools import cache
from typing import Any

import tiktoken

_ENCODING = "o200k_base"
_PER_MESSAGE_OVERHEAD = 4
_TRUNCATION_MARK = "\n[truncated]"


@cache
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(_ENCODING)


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def count_chat_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    total = 0
    for message in messages:
        total += _PER_MESSAGE_OVERHEAD
        total += count_tokens(json.dumps(message, ensure_ascii=False, default=str))
    if tools:
        total += count_tokens(json.dumps(tools, ensure_ascii=False))
    return total


def truncate_to_tokens(text: str, limit: int) -> str:
    """Return `text` cut so the whole result, including the mark, fits in `limit` tokens."""
    tokens = _encoding().encode(text, disallowed_special=())
    if len(tokens) <= limit:
        return text
    mark_len = count_tokens(_TRUNCATION_MARK)
    keep = max(limit - mark_len, 0)
    while keep >= 0:
        candidate = _encoding().decode(tokens[:keep]) + _TRUNCATION_MARK
        if count_tokens(candidate) <= limit:
            return candidate
        keep -= 1
    return ""
