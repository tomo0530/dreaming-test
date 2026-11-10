"""A directory of text files that a model may read and write, jailed to one root."""

import re
import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

MAX_GREP_MATCHES = 200
_MAX_GREP_LINE_CHARS = 300


class PathEscape(ValueError):
    """A path or pattern that points outside the store root."""


def _check_relative(rel: str) -> None:
    if "\0" in rel:
        raise ValueError("path contains a NUL byte")
    posix = PurePosixPath(rel)
    if posix.is_absolute() or ".." in posix.parts:
        raise PathEscape(f"path escapes the store root: {rel}")


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


class MemoryStore:
    """Files under `root`, addressed by POSIX paths relative to it.

    Every path is resolved with symlinks followed and must stay inside `root`;
    `..`, absolute paths, and symlinks that lead out raise `PathEscape`.
    Errors carry paths relative to the root, never absolute ones.

    Parameters
    ----------
    root
        Directory the store lives in. A writable store creates it.
    writable
        When false, `write`, `edit` and `restore` raise `PermissionError`.
    max_file_bytes
        Largest UTF-8 encoded file `write` and `edit` will produce.
    """

    def __init__(self, root: Path, *, writable: bool, max_file_bytes: int = 100_000) -> None:
        self._root = root
        self.writable = writable
        self.max_file_bytes = max_file_bytes
        if writable:
            root.mkdir(parents=True, exist_ok=True)

    def read(self, rel: str, *, offset: int = 0, limit: int = 200) -> str:
        """Return up to `limit` numbered lines starting after `offset` lines.

        The result ends with a `[lines a-b of N]` footer (`0-0` when the window is empty).
        """
        if offset < 0 or limit < 1:
            raise ValueError("offset must be >= 0 and limit must be >= 1")
        lines = _split_lines(self.read_all(rel))
        window = lines[offset : offset + limit]
        first = offset + 1 if window else 0
        last = offset + len(window) if window else 0
        footer = f"[lines {first}-{last} of {len(lines)}]"
        numbered = [f"{first + i}\t{line}" for i, line in enumerate(window)]
        return "\n".join([*numbered, footer])

    def read_all(self, rel: str) -> str:
        return self._existing_file(rel).read_text(encoding="utf-8")

    def write(self, rel: str, content: str) -> None:
        self._require_writable()
        self._check_size(content)
        target = self._resolve(rel)
        if target.is_dir():
            raise IsADirectoryError(f"is a directory: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def edit(self, rel: str, old: str, new: str) -> None:
        """Replace `old` by `new`, which must occur exactly once in the file."""
        self._require_writable()
        if not old:
            raise ValueError("old text must not be empty")
        target = self._existing_file(rel)
        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old)
        if occurrences == 0:
            raise ValueError(f"old text not found in {rel}")
        if occurrences > 1:
            raise ValueError(
                f"old text appears {occurrences} times in {rel}; it must match exactly once"
            )
        updated = content.replace(old, new, 1)
        self._check_size(updated)
        target.write_text(updated, encoding="utf-8")

    def exists(self, rel: str) -> bool:
        """True when `rel` names an existing regular file."""
        return self._resolve(rel).is_file()

    def glob(self, pattern: str) -> list[str]:
        """Sorted relative paths of the files matching `pattern`.

        Directories, and symlinks whose target lies outside the root, are not listed.
        """
        if not pattern:
            raise ValueError("glob pattern must not be empty")
        _check_relative(pattern)
        root = self._resolved_root()
        if not root.is_dir():
            return []
        matches = {
            path.relative_to(root).as_posix()
            for path in root.glob(pattern)
            if path.is_file() and path.resolve().is_relative_to(root)
        }
        return sorted(matches)

    def grep(self, pattern: str, glob: str = "**/*.md") -> list[str]:
        """Lines matching the regular expression `pattern`, as `path:line: text`.

        At most `MAX_GREP_MATCHES` lines are returned, each cut to 300 characters.
        """
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        matches: list[str] = []
        for rel in self.glob(glob):
            text = self._resolve(rel).read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(_split_lines(text), start=1):
                if not regex.search(line):
                    continue
                if len(line) > _MAX_GREP_LINE_CHARS:
                    line = line[:_MAX_GREP_LINE_CHARS] + "..."
                matches.append(f"{rel}:{number}: {line}")
                if len(matches) == MAX_GREP_MATCHES:
                    return matches
        return matches

    def snapshot(self, dest: Path) -> None:
        """Copy the whole store to `dest`, replacing whatever is there."""
        root = self._resolved_root()
        if not root.is_dir():
            raise FileNotFoundError("the store has no directory to snapshot")
        self._require_disjoint(dest)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(root, dest, symlinks=True)

    def restore(self, src: Path) -> None:
        """Replace the store content with the snapshot in `src`."""
        self._require_writable()
        if not src.is_dir():
            raise FileNotFoundError(f"snapshot directory not found: {src}")
        self._require_disjoint(src)
        root = self._resolved_root()
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(src, root, symlinks=True)

    def _resolved_root(self) -> Path:
        return self._root.resolve()

    def _resolve(self, rel: str) -> Path:
        _check_relative(rel)
        root = self._resolved_root()
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise PathEscape(f"path escapes the store root: {rel}")
        return target

    def _existing_file(self, rel: str) -> Path:
        target = self._resolve(rel)
        if target.is_dir():
            raise IsADirectoryError(f"is a directory: {rel}")
        if not target.is_file():
            raise FileNotFoundError(f"no such file: {rel}")
        return target

    def _require_writable(self) -> None:
        if not self.writable:
            raise PermissionError("this location is read-only")

    def _check_size(self, content: str) -> None:
        size = len(content.encode("utf-8"))
        if size > self.max_file_bytes:
            raise ValueError(f"content is {size} bytes; the limit is {self.max_file_bytes} bytes")

    def _require_disjoint(self, other: Path) -> None:
        root = self._resolved_root()
        resolved = other.resolve()
        if resolved.is_relative_to(root) or root.is_relative_to(resolved):
            raise ValueError("snapshot directory must not overlap the store root")


def markers_in_store(store: MemoryStore, markers: Sequence[str]) -> list[str]:
    """The `markers` that occur verbatim on a line of any file in `store`, in the given order.

    Used to tell whether an injected false note still survives in an arm's memory.
    """
    return [marker for marker in markers if store.grep(re.escape(marker), "**/*")]
