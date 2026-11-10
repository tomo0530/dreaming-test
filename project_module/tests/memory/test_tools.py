from pathlib import Path
from typing import Any

import pytest
from project_module.contracts import ToolCall
from project_module.memory.store import MemoryStore
from project_module.memory.tools import MAX_READ_RESULT_TOKENS, FileToolset, MemoryReadTools
from project_module.tokens import count_tokens


def _call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id="call_1", name=name, arguments=arguments)


def _toolset(tmp_path: Path) -> FileToolset:
    seed = MemoryStore(tmp_path / "transcripts", writable=True)
    seed.write("session_001.txt", "USER: hello\nAGENT: price_c is cents\n")
    memory = MemoryStore(tmp_path / "memory", writable=True)
    transcripts = MemoryStore(tmp_path / "transcripts", writable=False)
    return FileToolset({"memory": memory, "transcripts": transcripts})


def _assert_function_tool(schema: dict[str, Any]) -> None:
    assert schema["type"] == "function"
    function = schema["function"]
    assert set(function) == {"name", "description", "parameters"}
    assert len(function["description"]) > 30
    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert set(parameters["required"]) <= set(parameters["properties"])
    for prop in parameters["properties"].values():
        assert prop["type"] in {"string", "integer"}
        assert len(prop["description"]) > 10


def test_file_toolset_schemas_are_openai_function_tools(tmp_path: Path) -> None:
    schemas = _toolset(tmp_path).schemas()
    assert [s["function"]["name"] for s in schemas] == [
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
    ]
    for schema in schemas:
        _assert_function_tool(schema)
    read_file = schemas[0]["function"]
    assert "memory (read-write)" in read_file["description"]
    assert "transcripts (read-only)" in read_file["description"]
    assert read_file["parameters"]["required"] == ["path"]
    assert schemas[1]["function"]["parameters"]["required"] == ["path", "content"]
    assert schemas[2]["function"]["parameters"]["required"] == ["path", "old", "new"]


def test_file_toolset_routes_by_mount_prefix(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)

    written = tools.dispatch(_call("write_file", path="memory/notes/a.md", content="alpha\nbeta\n"))
    assert written.startswith("OK:")
    assert (tmp_path / "memory" / "notes" / "a.md").read_text() == "alpha\nbeta\n"

    assert tools.dispatch(_call("read_file", path="memory/notes/a.md", limit=1)) == (
        "1\talpha\n[lines 1-1 of 2]"
    )
    assert "price_c is cents" in tools.dispatch(
        _call("read_file", path="transcripts/session_001.txt")
    )

    assert tools.dispatch(
        _call("edit_file", path="memory/notes/a.md", old="beta", new="gamma")
    ) == ("OK: edited memory/notes/a.md")
    assert (tmp_path / "memory" / "notes" / "a.md").read_text() == "alpha\ngamma\n"

    assert tools.dispatch(_call("glob", pattern="memory/**/*.md")) == "memory/notes/a.md"
    assert tools.dispatch(_call("glob", pattern="transcripts/*")) == "transcripts/session_001.txt"
    assert tools.dispatch(_call("glob", pattern="memory")) == "memory/notes/a.md"
    assert tools.dispatch(_call("glob", pattern="memory/*.zzz")) == "(no matches)"


def test_file_toolset_grep_searches_all_mounts_unless_glob_is_given(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)
    tools.dispatch(_call("write_file", path="memory/notes/a.md", content="price_c: cents\n"))

    everywhere = tools.dispatch(_call("grep", pattern="price_c")).splitlines()
    assert everywhere == [
        "memory/notes/a.md:1: price_c: cents",
        "transcripts/session_001.txt:2: AGENT: price_c is cents",
    ]
    only_memory = tools.dispatch(_call("grep", pattern="price_c", glob="memory/**/*.md"))
    assert only_memory == "memory/notes/a.md:1: price_c: cents"
    assert tools.dispatch(_call("grep", pattern="absent")) == "(no matches)"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("no_such_tool", {}),
        ("read_file", {}),
        ("read_file", {"path": 5}),
        ("read_file", {"path": "memory/a.md", "offset": "1"}),
        ("read_file", {"path": "memory/a.md", "offset": True}),
        ("read_file", {"path": "memory/a.md", "offset": -1}),
        ("read_file", {"path": "memory/a.md", "limit": 0}),
        ("read_file", {"path": "memory/missing.md"}),
        ("read_file", {"path": "memory"}),
        ("read_file", {"path": "memory/"}),
        ("read_file", {"path": "elsewhere/a.md"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("read_file", {"path": "a.md"}),
        ("read_file", {"path": "memory/../outside.md"}),
        ("read_file", {"path": "memory//etc/passwd"}),
        ("read_file", {"path": "memory/a\0b.md"}),
        ("write_file", {"path": "memory/x.md"}),
        ("write_file", {"path": "memory/x.md", "content": 5}),
        ("write_file", {"path": "transcripts/x.md", "content": "no"}),
        ("write_file", {"path": "memory/../outside.md", "content": "no"}),
        ("write_file", {"path": "memory/big.md", "content": "x" * 200_000}),
        ("write_file", {"path": "memory/a.md/inside.md", "content": "parent is a file"}),
        ("write_file", {"path": "memory/lone.md", "content": "bad \ud83d surrogate"}),
        ("edit_file", {"path": "memory/a.md", "old": "dup", "new": "x"}),
        ("edit_file", {"path": "memory/a.md", "old": "absent", "new": "x"}),
        ("edit_file", {"path": "transcripts/session_001.txt", "old": "hello", "new": "x"}),
        ("glob", {"pattern": "**/*.md"}),
        ("glob", {"pattern": "memory/../*"}),
        ("glob", {"pattern": "/etc/*"}),
        ("grep", {"pattern": ""}),
        ("grep", {"pattern": "x", "glob": "**/*.md"}),
        ("grep", {"pattern": "x", "glob": 5}),
        ("grep", {}),
    ],
)
def test_file_toolset_returns_errors_as_text(
    tmp_path: Path, name: str, arguments: dict[str, Any]
) -> None:
    tools = _toolset(tmp_path)
    tools.dispatch(_call("write_file", path="memory/a.md", content="dup\ndup\n"))

    result = tools.dispatch(_call(name, **arguments))

    assert result.startswith("ERROR:")
    assert str(tmp_path) not in result
    assert not (tmp_path / "outside.md").exists()
    assert (tmp_path / "transcripts" / "session_001.txt").read_text().startswith("USER: hello")
    assert (tmp_path / "memory" / "a.md").read_text() == "dup\ndup\n"


def test_file_toolset_error_messages_explain_the_problem(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)
    read_only = tools.dispatch(_call("write_file", path="transcripts/x.md", content="no"))
    assert "read-only" in read_only
    unknown_mount = tools.dispatch(_call("read_file", path="elsewhere/a.md"))
    assert "memory" in unknown_mount and "transcripts" in unknown_mount
    escape = tools.dispatch(_call("read_file", path="memory/../outside.md"))
    assert "escapes" in escape


def test_file_toolset_rejects_invalid_mount_names(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m", writable=True)
    for name in ["", "a/b"]:
        with pytest.raises(ValueError, match="mount"):
            FileToolset({name: store})


def test_file_toolset_without_mount_cannot_reach_transcripts(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path / "memory", writable=True)
    tools = FileToolset({"memory": memory})
    result = tools.dispatch(_call("read_file", path="transcripts/session_001.txt"))
    assert result.startswith("ERROR:")


def _read_tools(
    tmp_path: Path, *, token_budget: int = 10_000, writable_prefix: str | None = None
) -> MemoryReadTools:
    store = MemoryStore(tmp_path / "memory", writable=True)
    if not store.exists("big.md"):
        store.write("big.md", "".join(f"line {i}: alpha beta gamma\n" for i in range(1, 41)))
        store.write("injected.md", "price_c is cents\n")
    return MemoryReadTools(store, token_budget=token_budget, writable_prefix=writable_prefix)


def test_memory_read_tools_schemas(tmp_path: Path) -> None:
    read_only = _read_tools(tmp_path).schemas()
    assert [s["function"]["name"] for s in read_only] == ["memory_read", "memory_search"]
    writable = _read_tools(tmp_path, writable_prefix="notes/").schemas()
    assert [s["function"]["name"] for s in writable] == [
        "memory_read",
        "memory_search",
        "memory_write",
    ]
    for schema in writable:
        _assert_function_tool(schema)
    assert "notes/" in writable[2]["function"]["description"]
    assert writable[2]["function"]["parameters"]["required"] == ["path", "content"]


def test_memory_read_and_search_use_paths_relative_to_the_memory_root(tmp_path: Path) -> None:
    tools = _read_tools(tmp_path)
    assert tools.dispatch(_call("memory_read", path="injected.md")) == (
        "1\tprice_c is cents\n[lines 1-1 of 1]"
    )
    assert tools.dispatch(_call("memory_search", pattern="price_c")) == (
        "injected.md:1: price_c is cents"
    )
    assert tools.dispatch(_call("memory_search", pattern="nothing")) == "(no matches)"
    for bad in ["../x.md", "/etc/passwd", "missing.md"]:
        assert tools.dispatch(_call("memory_read", path=bad)).startswith("ERROR:")
    assert tools.dispatch(_call("memory_search", pattern="")).startswith("ERROR:")
    assert tools.dispatch(_call("memory_search", pattern="(bad")) == "(no matches)"


def test_memory_read_tools_enforce_token_budget(tmp_path: Path) -> None:
    probe = _read_tools(tmp_path)
    five_lines = probe.dispatch(_call("memory_read", path="big.md", limit=5))
    one_line = probe.dispatch(_call("memory_read", path="big.md", limit=1))
    cost_five = count_tokens(five_lines)
    assert count_tokens(one_line) < cost_five // 2

    tools = _read_tools(tmp_path, token_budget=cost_five + cost_five // 2)
    assert tools.dispatch(_call("memory_read", path="big.md", limit=5)) == five_lines
    refused = tools.dispatch(_call("memory_read", path="big.md", offset=5, limit=5))
    assert refused.startswith("ERROR:") and "budget" in refused
    # A refused call is not charged, so a smaller read still fits; search is charged too.
    assert tools.dispatch(_call("memory_read", path="big.md", limit=1)).startswith("1\t")
    assert tools.dispatch(_call("memory_search", pattern="alpha")).startswith("ERROR:")

    fresh = _read_tools(tmp_path, token_budget=cost_five + cost_five // 2)
    assert fresh.dispatch(_call("memory_read", path="big.md", limit=5)) == five_lines


def test_failed_reads_do_not_consume_the_budget(tmp_path: Path) -> None:
    probe = _read_tools(tmp_path)
    cost = count_tokens(probe.dispatch(_call("memory_read", path="injected.md")))
    tools = _read_tools(tmp_path, token_budget=cost)
    for _ in range(5):
        assert tools.dispatch(_call("memory_read", path="missing.md")).startswith("ERROR:")
    assert tools.dispatch(_call("memory_read", path="injected.md")).startswith("1\t")


def test_memory_write_limited_to_prefix(tmp_path: Path) -> None:
    tools = _read_tools(tmp_path, writable_prefix="notes/")
    store = MemoryStore(tmp_path / "memory", writable=True)

    assert tools.dispatch(_call("memory_write", path="notes/a.md", content="a")).startswith("OK:")
    assert tools.dispatch(_call("memory_write", path="./notes/sub/b.md", content="b")).startswith(
        "OK:"
    )
    assert store.read_all("notes/a.md") == "a"
    assert store.read_all("notes/sub/b.md") == "b"

    for path in [
        "injected.md",
        "notes",
        "notes.md",
        "other/notes/a.md",
        "notes/../injected.md",
        "../notes/x.md",
        "/notes/x.md",
    ]:
        result = tools.dispatch(_call("memory_write", path=path, content="pwned"))
        assert result.startswith("ERROR:"), path
    assert store.read_all("injected.md") == "price_c is cents\n"
    assert not store.exists("notes.md")


def test_memory_write_prefix_without_trailing_slash_behaves_the_same(tmp_path: Path) -> None:
    tools = _read_tools(tmp_path, writable_prefix="notes")
    assert tools.dispatch(_call("memory_write", path="notes/a.md", content="a")).startswith("OK:")
    assert tools.dispatch(_call("memory_write", path="notesx/a.md", content="a")).startswith(
        "ERROR:"
    )


def test_memory_write_is_unknown_without_a_writable_prefix(tmp_path: Path) -> None:
    tools = _read_tools(tmp_path, writable_prefix=None)
    result = tools.dispatch(_call("memory_write", path="notes/a.md", content="a"))
    assert result.startswith("ERROR:")
    assert not (tmp_path / "memory" / "notes").exists()


def test_memory_write_reports_size_limit_errors(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory", writable=True, max_file_bytes=10)
    tools = MemoryReadTools(store, token_budget=100, writable_prefix="notes/")
    result = tools.dispatch(_call("memory_write", path="notes/a.md", content="x" * 11))
    assert result.startswith("ERROR:") and "limit" in result


@pytest.mark.parametrize("prefix", ["", "/abs", "../up", "notes/../x", "."])
def test_memory_read_tools_reject_invalid_prefix(tmp_path: Path, prefix: str) -> None:
    store = MemoryStore(tmp_path / "memory", writable=True)
    with pytest.raises(ValueError, match="prefix"):
        MemoryReadTools(store, token_budget=10, writable_prefix=prefix)


def test_memory_read_tools_reject_prefix_on_read_only_store(tmp_path: Path) -> None:
    MemoryStore(tmp_path / "memory", writable=True)
    store = MemoryStore(tmp_path / "memory", writable=False)
    with pytest.raises(ValueError, match="read-only"):
        MemoryReadTools(store, token_budget=10, writable_prefix="notes/")


def test_file_toolset_grep_is_a_case_insensitive_literal_search(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)
    tools.dispatch(
        _call("write_file", path="memory/a.md", content="MOVED TO [[x]]\nOrders.Status (a+)+$\n")
    )

    assert tools.dispatch(_call("grep", pattern="moved to", glob="memory/*.md")) == (
        "memory/a.md:1: MOVED TO [[x]]"
    )
    # Regular-expression syntax is plain text: no anchors, groups or quantifiers.
    assert tools.dispatch(_call("grep", pattern="^MOVED TO", glob="memory/*.md")) == "(no matches)"
    assert tools.dispatch(_call("grep", pattern="(a+)+$", glob="memory/*.md")) == (
        "memory/a.md:2: Orders.Status (a+)+$"
    )
    assert tools.dispatch(_call("grep", pattern="orders.status", glob="memory/*.md")) == (
        "memory/a.md:2: Orders.Status (a+)+$"
    )


def test_file_toolset_grep_handles_a_catastrophic_regex_pattern_quickly(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)
    tools.dispatch(_call("write_file", path="memory/a.md", content="a" * 90_000 + "!\n"))

    assert tools.dispatch(_call("grep", pattern="(a+)+$", glob="memory/*.md")) == "(no matches)"


def _long_lines(count: int) -> str:
    return "".join(f"line {i} " + "word " * 30 + "\n" for i in range(1, count + 1))


def test_read_file_cuts_a_long_result_at_whole_lines_with_a_notice(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)
    tools.dispatch(_call("write_file", path="memory/big.md", content=_long_lines(400)))

    result = tools.dispatch(_call("read_file", path="memory/big.md", limit=400))

    assert count_tokens(result) <= MAX_READ_RESULT_TOKENS
    body, footer, notice = result.rsplit("\n", 2)
    shown = int(footer.split("-")[1].split(" ")[0])
    assert 0 < shown < 400
    assert footer == f"[lines 1-{shown} of 400]"
    assert body.split("\n")[-1].startswith(f"{shown}\tline {shown} ")
    assert f"`offset` {shown}" in notice and "200 lines or fewer" in notice
    resumed = tools.dispatch(_call("read_file", path="memory/big.md", offset=shown, limit=1))
    assert resumed.startswith(f"{shown + 1}\tline {shown + 1} ")


def test_read_file_cuts_a_single_overlong_line(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)
    tools.dispatch(_call("write_file", path="memory/wide.md", content="w " * 30_000 + "\nnext\n"))

    result = tools.dispatch(_call("read_file", path="memory/wide.md"))

    assert count_tokens(result) <= MAX_READ_RESULT_TOKENS
    assert result.startswith("1\tw w ")
    assert result.endswith("read on with `offset` 1]")


def test_read_file_leaves_a_result_within_the_cap_untouched(tmp_path: Path) -> None:
    tools = _toolset(tmp_path)
    tools.dispatch(_call("write_file", path="memory/small.md", content=_long_lines(3)))

    result = tools.dispatch(_call("read_file", path="memory/small.md"))

    assert result.endswith("[lines 1-3 of 3]")


def test_memory_read_is_cut_at_the_cap_and_charged_for_what_it_returns(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m", writable=True)
    store.write("big.md", _long_lines(400))
    tools = MemoryReadTools(store, token_budget=50_000, writable_prefix=None)

    result = tools.dispatch(_call("memory_read", path="big.md", limit=400))

    assert count_tokens(result) <= MAX_READ_RESULT_TOKENS
    assert "`offset`" in result
