import hashlib
import sqlite3
from pathlib import Path

import pytest
from project_module.schemadrift.sql import ALLOWED_PRAGMAS, SqlError, run_sql_readonly


def _make_db(path: Path, rows: int = 2000) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"row {i}") for i in range(rows)])
    conn.commit()
    conn.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_run_sql_readonly_limits(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "db.sqlite")
    before = _digest(db)

    with pytest.raises(SqlError, match="timed out"):
        run_sql_readonly(db, "SELECT COUNT(*) FROM t a, t b, t c", row_limit=50, timeout_s=0.2)
    capped = run_sql_readonly(db, "SELECT id, label FROM t ORDER BY id", row_limit=50, timeout_s=5)

    assert capped.columns == ["id", "label"]
    assert len(capped.rows) == 50
    assert capped.rows[0] == [0, "row 0"]
    assert capped.truncated is True
    assert _digest(db) == before


def test_run_sql_readonly_returns_untruncated_result_within_limit(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "db.sqlite", rows=3)

    result = run_sql_readonly(db, "SELECT COUNT(*) AS n FROM t", row_limit=50, timeout_s=5)

    assert (result.columns, result.rows, result.truncated) == (["n"], [[3]], False)


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH DATABASE ':memory:' AS other",
        "DETACH DATABASE main",
        "INSERT INTO t VALUES (99999, 'x')",
        "UPDATE t SET label = 'x'",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE u (x INTEGER)",
        "CREATE TEMP TABLE u (x INTEGER)",
        "PRAGMA journal_mode=WAL",
        "PRAGMA foreign_keys = ON",
        "BEGIN",
    ],
    ids=[
        "attach",
        "detach",
        "insert",
        "update",
        "delete",
        "drop",
        "create",
        "create-temp",
        "pragma-journal-write",
        "pragma-setting-write",
        "transaction",
    ],
)
def test_run_sql_rejects_attach_and_writes(tmp_path: Path, sql: str) -> None:
    db = _make_db(tmp_path / "db.sqlite", rows=3)
    before = _digest(db)

    with pytest.raises(SqlError):
        run_sql_readonly(db, sql, row_limit=50, timeout_s=5)

    assert _digest(db) == before


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA table_info(t)",
        "SELECT name FROM pragma_table_info('t')",
        "SELECT name FROM sqlite_master WHERE type = 'table'",
        "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r WHERE x < 3) SELECT x FROM r",
    ],
    ids=["pragma-table-info", "pragma-function", "sqlite-master", "recursive-cte"],
)
def test_run_sql_allows_schema_introspection(tmp_path: Path, sql: str) -> None:
    db = _make_db(tmp_path / "db.sqlite", rows=3)

    result = run_sql_readonly(db, sql, row_limit=50, timeout_s=5)

    assert result.rows


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT missing FROM t", "no such column"),
        ("SELECT 1; SELECT 2", "one statement"),
        ("", "no result"),
        ("SELECT zeroblob(5000000)", "too big"),
    ],
    ids=["unknown-column", "two-statements", "empty", "oversized-value"],
)
def test_run_sql_reports_errors_as_sql_error(tmp_path: Path, sql: str, message: str) -> None:
    db = _make_db(tmp_path / "db.sqlite", rows=3)

    with pytest.raises(SqlError, match=message):
        run_sql_readonly(db, sql, row_limit=50, timeout_s=5)


def test_run_sql_reports_a_missing_database_as_sql_error(tmp_path: Path) -> None:
    with pytest.raises(SqlError, match="cannot open database"):
        run_sql_readonly(tmp_path / "missing.sqlite", "SELECT 1", row_limit=50, timeout_s=5)


def test_run_sql_requires_a_positive_row_limit(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "db.sqlite", rows=3)

    with pytest.raises(ValueError, match="row_limit"):
        run_sql_readonly(db, "SELECT 1", row_limit=0, timeout_s=5)


@pytest.mark.parametrize("name", sorted(ALLOWED_PRAGMAS))
@pytest.mark.parametrize("form", ["PRAGMA {name}('t')", "SELECT * FROM pragma_{name}('t')"])
def test_run_sql_allows_each_allowlisted_pragma_in_both_forms(
    tmp_path: Path, name: str, form: str
) -> None:
    db = _make_db(tmp_path / "db.sqlite", rows=3)

    result = run_sql_readonly(db, form.format(name=name), row_limit=50, timeout_s=5)

    assert not result.truncated


@pytest.mark.parametrize(
    "name", ["database_list", "compile_options", "user_version", "table_list", "function_list"]
)
@pytest.mark.parametrize("form", ["PRAGMA {name}", "SELECT * FROM pragma_{name}"])
def test_run_sql_refuses_other_pragmas_in_both_forms(tmp_path: Path, name: str, form: str) -> None:
    db = _make_db(tmp_path / "db.sqlite", rows=3)

    with pytest.raises(SqlError):
        run_sql_readonly(db, form.format(name=name), row_limit=50, timeout_s=5)


def test_allowed_pragmas_are_exactly_the_schema_introspection_set() -> None:
    assert ALLOWED_PRAGMAS == {
        "table_info",
        "table_xinfo",
        "index_list",
        "index_info",
        "foreign_key_list",
    }
