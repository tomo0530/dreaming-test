"""Function-calling tool sets over `MemoryStore`.

Both tool sets satisfy `ToolSet`: `dispatch` never raises and reports every failure
as text starting with ``ERROR:``.
"""

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from project_module.contracts import ToolCall, ToolSet
from project_module.memory.store import MAX_GREP_MATCHES, MemoryStore
from project_module.tokens import count_tokens, truncate_to_tokens
from project_module.tool_names import MEMORY_READ_TOOL, MEMORY_SEARCH_TOOL, MEMORY_WRITE_TOOL

NO_MATCHES = "(no matches)"
# Largest single result of `read_file` and `memory_read`; longer reads are cut.
MAX_READ_RESULT_TOKENS = 8_000
_ALL_FILES = "**/*"
_DEFAULT_LIMIT = 200
_READ_CUT_NOTICE = (
    "[result cut at {limit} tokens: only the lines above were returned; read on with "
    "`offset` {offset} and a smaller `limit` (200 lines or fewer)]"
)
_LONG_LINE_NOTICE = (
    "[result cut at {limit} tokens: line {line} alone is longer than that and was cut; "
    "read on with `offset` {offset}]"
)

_Handler = Callable[[dict[str, Any]], str]


def _function(
    name: str, description: str, properties: dict[str, dict[str, Any]], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _integer(description: str, *, default: int, minimum: int) -> dict[str, Any]:
    return {"type": "integer", "description": description, "default": default, "minimum": minimum}


_OFFSET = _integer(
    "Number of lines to skip before reading (0-based). Use it to read the next page.",
    default=0,
    minimum=0,
)
_LIMIT = _integer(
    "Maximum number of lines to return per call; use 200 or fewer. A result longer than "
    f"{MAX_READ_RESULT_TOKENS} tokens is cut.",
    default=_DEFAULT_LIMIT,
    minimum=1,
)
_SEARCH_NOTE = "a piece of literal text (not a regular expression; case is ignored)"


def _string_arg(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"argument '{name}' is required and must be a string")
    return value


def _optional_string_arg(arguments: dict[str, Any], name: str) -> str | None:
    if arguments.get(name) is None:
        return None
    return _string_arg(arguments, name)


def _int_arg(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"argument '{name}' must be an integer")
    return value


def _describe(exc: OSError | ValueError) -> str:
    # Raw OS errors embed absolute paths in str(exc); the strerror alone is enough for the model.
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc)


def _lines_or_none(lines: list[str]) -> str:
    return "\n".join(lines) if lines else NO_MATCHES


def _read_capped(store: MemoryStore, rel: str, *, offset: int, limit: int) -> str:
    """`store.read`, cut to whole lines that fit in `MAX_READ_RESULT_TOKENS` with a notice.

    The notice names the `offset` to continue from. A first line that is too long on its own
    is returned cut, and the notice points past it.
    """
    result = store.read(rel, offset=offset, limit=limit)
    if count_tokens(result) <= MAX_READ_RESULT_TOKENS:
        return result

    def cut(lines: int) -> str:
        notice = _READ_CUT_NOTICE.format(limit=MAX_READ_RESULT_TOKENS, offset=offset + lines)
        return f"{store.read(rel, offset=offset, limit=lines)}\n{notice}"

    # Invariant: `low` lines fit with the notice (or `low` is 0) and `high` lines do not.
    low, high = 0, limit
    while high - low > 1:
        middle = (low + high) // 2
        if count_tokens(cut(middle)) <= MAX_READ_RESULT_TOKENS:
            low = middle
        else:
            high = middle
    if low > 0:
        return cut(low)
    notice = _LONG_LINE_NOTICE.format(
        limit=MAX_READ_RESULT_TOKENS, line=offset + 1, offset=offset + 1
    )
    first = store.read(rel, offset=offset, limit=1).split("\n", 1)[0]
    room = MAX_READ_RESULT_TOKENS - count_tokens(notice) - 1
    return f"{truncate_to_tokens(first, room)}\n{notice}"


class _HandlerToolSet(ToolSet):
    """Dispatch by tool name to handlers that may raise; failures become ERROR text."""

    def __init__(self, schemas: list[dict[str, Any]], handlers: dict[str, _Handler]) -> None:
        self._schemas = schemas
        self._handlers = handlers

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def dispatch(self, call: ToolCall) -> str:
        handler = self._handlers.get(call.name)
        if handler is None:
            return f"ERROR: unknown tool '{call.name}'"
        try:
            return handler(call.arguments)
        except (OSError, ValueError) as exc:
            return f"ERROR: {_describe(exc)}"


class FileToolset(_HandlerToolSet):
    """The five file tools of the consolidation loops, over named mounts.

    Every path (and every glob pattern) starts with a mount name, for example
    ``memory/notes/a.md``. Results are prefixed the same way, so they can be passed
    straight back to the tools.

    Parameters
    ----------
    mounts
        Mount name to store, e.g. ``{"memory": ..., "transcripts": ...}``.
    """

    def __init__(self, mounts: dict[str, MemoryStore]) -> None:
        for name in mounts:
            if not name or "/" in name:
                raise ValueError(f"invalid mount name: {name!r}")
        self._mounts = dict(mounts)
        super().__init__(
            self._build_schemas(),
            {
                "read_file": self._read_file,
                "write_file": self._write_file,
                "edit_file": self._edit_file,
                "glob": self._glob,
                "grep": self._grep,
            },
        )

    def _build_schemas(self) -> list[dict[str, Any]]:
        listing = ", ".join(
            f"{name} ({'read-write' if store.writable else 'read-only'})"
            for name, store in self._mounts.items()
        )
        where = f"Every path starts with a mount name. Mounts: {listing}."
        return [
            _function(
                "read_file",
                "Read a text file and return its lines with 1-based line numbers, followed by "
                "a final line `[lines a-b of N]`. Read long files in pages of at most 200 lines "
                f"with `offset` and `limit`. {where}",
                {
                    "path": _string("File to read, e.g. `memory/notes/orders.md`."),
                    "offset": _OFFSET,
                    "limit": _LIMIT,
                },
                ["path"],
            ),
            _function(
                "write_file",
                "Create a file, or replace it completely, with `content`. Missing parent "
                "directories are created. Fails on read-only mounts and when the content "
                f"exceeds the file size limit. {where}",
                {
                    "path": _string("File to write, e.g. `memory/wiki/units.md`."),
                    "content": _string("The complete new content of the file."),
                },
                ["path", "content"],
            ),
            _function(
                "edit_file",
                "Replace one exact piece of text in an existing file. `old` must occur exactly "
                "once in the file, so include enough surrounding text to make it unique; the "
                f"call fails if it occurs zero or several times. {where}",
                {
                    "path": _string("File to edit."),
                    "old": _string("The exact text to replace. It must occur exactly once."),
                    "new": _string("The replacement text."),
                },
                ["path", "old", "new"],
            ),
            _function(
                "glob",
                "List the files that match a glob pattern, one path per line. The pattern starts "
                f"with a mount name, e.g. `memory/**/*.md`. {where}",
                {"pattern": _string("Glob pattern, e.g. `transcripts/*.txt`.")},
                ["pattern"],
            ),
            _function(
                "grep",
                f"Search file contents for {_SEARCH_NOTE}. Returns at most {MAX_GREP_MATCHES} "
                "lines that contain it, as `path:line: text`. Without `glob`, every file in every "
                f"mount is searched. {where}",
                {
                    "pattern": _string("The literal text to search for, e.g. `orders.status`."),
                    "glob": _string(
                        "Optional glob that limits the files searched and starts with a mount "
                        "name, e.g. `memory/notes/*.md`."
                    ),
                },
                ["pattern"],
            ),
        ]

    def _locate(self, path: str) -> tuple[str, MemoryStore, str]:
        mount, _, rel = path.partition("/")
        store = self._mounts.get(mount)
        if store is None:
            names = ", ".join(self._mounts)
            raise ValueError(f"'{path}' must start with a mount name ({names}), e.g. 'memory/x.md'")
        return mount, store, rel

    def _read_file(self, arguments: dict[str, Any]) -> str:
        _, store, rel = self._locate(_string_arg(arguments, "path"))
        offset = _int_arg(arguments, "offset", 0)
        limit = _int_arg(arguments, "limit", _DEFAULT_LIMIT)
        return _read_capped(store, rel, offset=offset, limit=limit)

    def _write_file(self, arguments: dict[str, Any]) -> str:
        path = _string_arg(arguments, "path")
        content = _string_arg(arguments, "content")
        _, store, rel = self._locate(path)
        store.write(rel, content)
        return f"OK: wrote {len(content.encode('utf-8'))} bytes to {path}"

    def _edit_file(self, arguments: dict[str, Any]) -> str:
        path = _string_arg(arguments, "path")
        old = _string_arg(arguments, "old")
        new = _string_arg(arguments, "new")
        _, store, rel = self._locate(path)
        store.edit(rel, old, new)
        return f"OK: edited {path}"

    def _glob(self, arguments: dict[str, Any]) -> str:
        mount, store, rel = self._locate(_string_arg(arguments, "pattern"))
        return _lines_or_none([f"{mount}/{path}" for path in store.glob(rel or _ALL_FILES)])

    def _grep(self, arguments: dict[str, Any]) -> str:
        pattern = _string_arg(arguments, "pattern")
        glob = _optional_string_arg(arguments, "glob")
        if glob is None:
            targets = [(name, store, _ALL_FILES) for name, store in self._mounts.items()]
        else:
            mount, store, rel = self._locate(glob)
            targets = [(mount, store, rel or _ALL_FILES)]
        lines = [
            f"{mount}/{line}" for mount, store, rel in targets for line in store.grep(pattern, rel)
        ]
        return _lines_or_none(lines[:MAX_GREP_MATCHES])


class MemoryReadTools(_HandlerToolSet):
    """Memory access for the online agent and probes; build a new one for every question.

    Paths are relative to the memory root. Everything `memory_read` and `memory_search`
    return counts against `token_budget`; a call whose result no longer fits is refused
    and not charged. `memory_write` exists only when `writable_prefix` is given and
    accepts only files below that directory.

    Parameters
    ----------
    store
        The memory directory.
    token_budget
        Total tokens of read results this instance may return.
    writable_prefix
        Directory such as ``"notes/"`` that `memory_write` is limited to, or None for no
        write tool.
    """

    def __init__(
        self, store: MemoryStore, *, token_budget: int, writable_prefix: str | None
    ) -> None:
        self._store = store
        self._token_budget = token_budget
        self._tokens_used = 0
        handlers: dict[str, _Handler] = {
            MEMORY_READ_TOOL: self._read,
            MEMORY_SEARCH_TOOL: self._search,
        }
        schemas = self._read_schemas()
        # Empty unless `memory_write` is registered, which is the only reader of it.
        self._write_dir: tuple[str, ...] = ()
        if writable_prefix is not None:
            self._write_dir = self._parse_write_dir(store, writable_prefix)
            handlers[MEMORY_WRITE_TOOL] = self._write
            schemas.append(self._write_schema(self._write_dir))
        super().__init__(schemas, handlers)

    @staticmethod
    def _parse_write_dir(store: MemoryStore, prefix: str) -> tuple[str, ...]:
        if not store.writable:
            raise ValueError("writable_prefix requires a writable store; this one is read-only")
        posix = PurePosixPath(prefix)
        if not posix.parts or posix.is_absolute() or ".." in posix.parts:
            raise ValueError(f"writable_prefix must be a relative directory name, got {prefix!r}")
        return posix.parts

    def _read_schemas(self) -> list[dict[str, Any]]:
        budget = (
            f"The total amount of text you can read per question is limited to about "
            f"{self._token_budget} tokens; a call that would exceed it returns an error."
        )
        return [
            _function(
                MEMORY_READ_TOOL,
                "Read a memory file and return its lines with 1-based line numbers, followed by "
                f"a final line `[lines a-b of N]`. {budget}",
                {
                    "path": _string("Path relative to the memory root, e.g. `injected.md`."),
                    "offset": _OFFSET,
                    "limit": _LIMIT,
                },
                ["path"],
            ),
            _function(
                MEMORY_SEARCH_TOOL,
                f"Search all memory files for {_SEARCH_NOTE}. Returns at most {MAX_GREP_MATCHES} "
                f"lines that contain it, as `path:line: text`. {budget}",
                {"pattern": _string("The literal text to search for, e.g. a column name.")},
                ["pattern"],
            ),
        ]

    @staticmethod
    def _write_schema(write_dir: tuple[str, ...]) -> dict[str, Any]:
        directory = "/".join(write_dir) + "/"
        return _function(
            MEMORY_WRITE_TOOL,
            f"Create a note, or replace it completely, with `content`. Only files below "
            f"`{directory}` can be written, e.g. `{directory}orders-join.md`; other paths "
            "return an error.",
            {
                "path": _string(f"Path below `{directory}`, relative to the memory root."),
                "content": _string("The complete new content of the file."),
            },
            ["path", "content"],
        )

    def _charge(self, result: str) -> str:
        tokens = count_tokens(result)
        remaining = self._token_budget - self._tokens_used
        if tokens > remaining:
            raise ValueError(
                f"read budget exceeded: this result needs {tokens} tokens but only {remaining} "
                f"of {self._token_budget} remain; request fewer lines or a narrower pattern"
            )
        self._tokens_used += tokens
        return result

    def _read(self, arguments: dict[str, Any]) -> str:
        path = _string_arg(arguments, "path")
        offset = _int_arg(arguments, "offset", 0)
        limit = _int_arg(arguments, "limit", _DEFAULT_LIMIT)
        return self._charge(_read_capped(self._store, path, offset=offset, limit=limit))

    def _search(self, arguments: dict[str, Any]) -> str:
        pattern = _string_arg(arguments, "pattern")
        return self._charge(_lines_or_none(self._store.grep(pattern)))

    def _write(self, arguments: dict[str, Any]) -> str:
        path = _string_arg(arguments, "path")
        content = _string_arg(arguments, "content")
        parts = PurePosixPath(path).parts
        depth = len(self._write_dir)
        if len(parts) <= depth or parts[:depth] != self._write_dir:
            raise ValueError(f"writes are limited to files below '{'/'.join(self._write_dir)}/'")
        self._store.write(path, content)
        return f"OK: wrote {len(content.encode('utf-8'))} bytes to {path}"
