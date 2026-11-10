"""Read-only, time- and row-limited SQL execution against a stream's SQLite files."""

import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Pragmas that only describe the schema. Any other pragma is allowed only in its
# argument-free (read) form, so `PRAGMA x = y` style writes are refused.
_INTROSPECTION_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "table_list",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
    }
)
_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)
_PROGRESS_STEPS = 1_000
# Caps any single string or blob, so e.g. zeroblob(1e9) cannot exhaust memory.
_MAX_VALUE_BYTES = 1_000_000


class SqlResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


class SqlError(RuntimeError):
    """The statement failed, was refused, or ran past its time limit."""


def _authorize(
    action: int, arg1: str | None, arg2: str | None, db_name: str | None, source: str | None
) -> int:
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in _INTROSPECTION_PRAGMAS or arg2 is None:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run_sql_readonly(db_path: Path, sql: str, *, row_limit: int, timeout_s: float) -> SqlResult:
    """Run one read-only statement and return at most `row_limit` rows.

    Parameters
    ----------
    db_path
        SQLite file, opened with `mode=ro` so the file can never change.
    sql
        A single statement. Writes, ATTACH/DETACH, transactions and pragma writes
        are refused by an authorizer.
    row_limit
        Maximum rows returned; `truncated` tells whether more rows existed.
    timeout_s
        Wall-clock budget for executing and fetching.

    Raises
    ------
    SqlError
        On any SQLite error, a refused statement, a timeout, or a statement that
        produces no result set.
    """
    if row_limit < 1:
        raise ValueError("row_limit must be positive")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    deadline = time.monotonic() + timeout_s
    try:
        conn = sqlite3.connect(uri, uri=True, autocommit=True)
    except sqlite3.Error as err:
        raise SqlError(f"cannot open database: {err}") from err
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
        conn.set_authorizer(_authorize)
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), _PROGRESS_STEPS)
        try:
            cursor = conn.execute(sql)
            if cursor.description is None:
                raise SqlError("the statement returned no result set")
            fetched = cursor.fetchmany(row_limit + 1)
        except sqlite3.OperationalError as err:
            if time.monotonic() > deadline:
                raise SqlError(f"query timed out after {timeout_s:g} s") from err
            raise SqlError(str(err)) from err
        except sqlite3.Error as err:
            raise SqlError(str(err)) from err
        columns = [str(d[0]) for d in cursor.description]
    finally:
        conn.close()
    return SqlResult(
        columns=columns,
        rows=[list(row) for row in fetched[:row_limit]],
        truncated=len(fetched) > row_limit,
    )
