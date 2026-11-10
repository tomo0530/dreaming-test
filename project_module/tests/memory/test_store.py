import shutil
from pathlib import Path

import pytest
from project_module.memory.store import MemoryStore, PathEscape


def _store(tmp_path: Path, *, writable: bool = True, max_file_bytes: int = 100_000) -> MemoryStore:
    return MemoryStore(tmp_path / "memory", writable=writable, max_file_bytes=max_file_bytes)


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("top secret\n")
    return outside


def test_store_rejects_escape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = _outside(tmp_path)
    (store._root / "file-link.md").symlink_to(outside / "secret.md")
    (store._root / "dir-link").symlink_to(outside, target_is_directory=True)
    (store._root / "dangling.md").symlink_to(outside / "new.md")
    escapes = [
        "../x.md",
        "a/../../x.md",
        "notes/../x.md",
        "/etc/passwd",
        str(outside / "secret.md"),
        "file-link.md",
        "dir-link/secret.md",
        "dir-link/new.md",
        "dangling.md",
    ]
    for rel in escapes:
        with pytest.raises(PathEscape):
            store.read(rel)
        with pytest.raises(PathEscape):
            store.read_all(rel)
        with pytest.raises(PathEscape):
            store.write(rel, "pwned")
        with pytest.raises(PathEscape):
            store.edit(rel, "top", "pwned")
        with pytest.raises(PathEscape):
            store.exists(rel)
    assert (outside / "secret.md").read_text() == "top secret\n"
    assert not (outside / "new.md").exists()
    assert not (tmp_path / "x.md").exists()


def test_store_rejects_nul_byte_in_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="NUL"):
        store.read("a\0b.md")


def test_store_allows_symlink_that_stays_inside_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("real.md", "inside\n")
    (store._root / "alias.md").symlink_to(store._root / "real.md")
    assert store.read_all("alias.md") == "inside\n"


def test_store_write_creates_parents_and_read_all_roundtrips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("notes/a/b.md", "héllo\n")
    assert store.read_all("notes/a/b.md") == "héllo\n"
    assert store.exists("notes/a/b.md")
    assert not store.exists("notes/missing.md")
    assert not store.exists("notes")


def test_store_missing_file_and_directory_errors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("notes/a.md", "x\n")
    with pytest.raises(FileNotFoundError, match="missing.md"):
        store.read_all("missing.md")
    with pytest.raises(IsADirectoryError, match="notes"):
        store.read_all("notes")
    with pytest.raises(IsADirectoryError):
        store.write("notes", "x")


def test_store_read_paginates_with_line_numbers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "".join(f"line {i}\n" for i in range(1, 11)))
    assert store.read("a.md", limit=3) == "1\tline 1\n2\tline 2\n3\tline 3\n[lines 1-3 of 10]"
    assert store.read("a.md", offset=8, limit=5) == "9\tline 9\n10\tline 10\n[lines 9-10 of 10]"
    assert store.read("a.md").endswith("[lines 1-10 of 10]")


def test_store_read_handles_empty_file_and_offset_past_end(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("empty.md", "")
    store.write("a.md", "only\n")
    assert store.read("empty.md") == "[lines 0-0 of 0]"
    assert store.read("a.md", offset=5) == "[lines 0-0 of 1]"


def test_store_read_counts_a_last_line_without_trailing_newline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "first\nsecond")
    assert store.read("a.md") == "1\tfirst\n2\tsecond\n[lines 1-2 of 2]"


def test_store_read_rejects_bad_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "x\n")
    with pytest.raises(ValueError):
        store.read("a.md", offset=-1)
    with pytest.raises(ValueError):
        store.read("a.md", limit=0)


def test_store_edit_requires_unique_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "alpha beta\nbeta gamma\n")
    with pytest.raises(ValueError, match="2 times"):
        store.edit("a.md", "beta", "delta")
    with pytest.raises(ValueError, match="not found"):
        store.edit("a.md", "zeta", "delta")
    with pytest.raises(ValueError, match="empty"):
        store.edit("a.md", "", "delta")
    assert store.read_all("a.md") == "alpha beta\nbeta gamma\n"
    store.edit("a.md", "alpha", "ALPHA")
    assert store.read_all("a.md") == "ALPHA beta\nbeta gamma\n"


def test_store_enforces_max_file_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path, max_file_bytes=10)
    store.write("a.md", "1234567890")
    with pytest.raises(ValueError, match="limit"):
        store.write("b.md", "12345678901")
    with pytest.raises(ValueError, match="limit"):
        store.edit("a.md", "1", "11")
    assert store.read_all("a.md") == "1234567890"
    assert not store.exists("b.md")
    # The limit counts encoded bytes, not characters.
    with pytest.raises(ValueError, match="limit"):
        store.write("c.md", "é" * 6)


def test_store_read_only_rejects_writes(tmp_path: Path) -> None:
    _store(tmp_path).write("a.md", "x\n")
    store = _store(tmp_path, writable=False)
    assert store.read_all("a.md") == "x\n"
    with pytest.raises(PermissionError, match="read-only"):
        store.write("a.md", "y")
    with pytest.raises(PermissionError, match="read-only"):
        store.edit("a.md", "x", "y")
    with pytest.raises(PermissionError, match="read-only"):
        store.restore(tmp_path)
    assert store.read_all("a.md") == "x\n"


def test_store_read_only_on_missing_root_lists_nothing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "absent", writable=False)
    assert store.glob("**/*") == []
    assert store.grep("x") == []
    assert not (tmp_path / "absent").exists()


def test_store_glob_lists_files_sorted_and_skips_escaping_symlinks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = _outside(tmp_path)
    store.write("b.md", "b")
    store.write("notes/a.md", "a")
    store.write("notes/deep/c.md", "c")
    store.write("notes/skip.txt", "t")
    (store._root / "leak.md").symlink_to(outside / "secret.md")
    (store._root / "leakdir").symlink_to(outside, target_is_directory=True)
    assert store.glob("**/*.md") == ["b.md", "notes/a.md", "notes/deep/c.md"]
    assert store.glob("notes/*.md") == ["notes/a.md"]
    assert store.glob("*/secret.md") == []
    assert store.glob("nothing/*") == []


def test_store_glob_rejects_escaping_patterns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for pattern in ["../*", "/etc/*", "notes/../../*", ""]:
        with pytest.raises(ValueError):
            store.glob(pattern)


def test_store_grep_formats_matches_and_honours_glob(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "alpha\nprice_c is cents\n")
    store.write("notes/b.md", "nothing\nprice_c again\n")
    store.write("notes/c.txt", "price_c in text\n")
    assert store.grep("price_c") == ["a.md:2: price_c is cents", "notes/b.md:2: price_c again"]
    assert store.grep("price_c", glob="notes/*") == [
        "notes/b.md:2: price_c again",
        "notes/c.txt:1: price_c in text",
    ]
    assert store.grep("(?i)ALPHA") == ["a.md:1: alpha"]
    assert store.grep("absent") == []


def test_store_grep_caps_matches_and_line_length(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("many.md", "hit\n" * 500)
    store.write("long.md", "hit " + "x" * 1000 + "\n")
    lines = store.grep("hit")
    assert len(lines) == 200
    assert all(len(line) < 400 for line in store.grep("hit", glob="long.md"))


def test_store_grep_rejects_invalid_regex(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "x\n")
    with pytest.raises(ValueError, match="regular expression"):
        store.grep("(unclosed")


def test_store_snapshot_and_restore_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("injected.md", "v1\n")
    store.write("notes/a.md", "a1\n")
    snap = tmp_path / "snap" / "memory"
    store.snapshot(snap)

    store.write("injected.md", "v2\n")
    store.write("notes/new.md", "new\n")
    store.restore(snap)

    assert store.read_all("injected.md") == "v1\n"
    assert store.glob("**/*.md") == ["injected.md", "notes/a.md"]


def test_store_snapshot_replaces_the_previous_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("keep.md", "1\n")
    store.write("gone.md", "x\n")
    snap = tmp_path / "snap"
    store.snapshot(snap)

    (store._root / "gone.md").unlink()
    store.write("keep.md", "2\n")
    store.snapshot(snap)

    assert not (snap / "gone.md").exists()
    assert (snap / "keep.md").read_text() == "2\n"


def test_store_restore_from_empty_snapshot_clears_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "x\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    store.restore(empty)
    assert store.glob("**/*") == []
    store.write("b.md", "still writable\n")


def test_store_restore_recreates_a_deleted_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "x\n")
    snap = tmp_path / "snap"
    store.snapshot(snap)
    shutil.rmtree(store._root)
    store.restore(snap)
    assert store.read_all("a.md") == "x\n"


def test_store_snapshot_of_a_missing_read_only_root_fails(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "absent", writable=False)
    with pytest.raises(FileNotFoundError):
        store.snapshot(tmp_path / "snap")


def test_store_snapshot_and_restore_reject_missing_or_overlapping_dirs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("a.md", "x\n")
    with pytest.raises(FileNotFoundError):
        store.restore(tmp_path / "no-such-snapshot")
    for overlapping in [store._root, store._root / "inner", tmp_path]:
        with pytest.raises(ValueError, match="overlap"):
            store.snapshot(overlapping)
    assert store.read_all("a.md") == "x\n"
