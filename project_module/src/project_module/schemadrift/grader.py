"""Execution-match grading with stale-answer detection for SchemaDrift-SQL."""

import math
from numbers import Real
from pathlib import Path
from typing import Any

from project_module.contracts import Grade
from project_module.schemadrift.models import Question, Stream
from project_module.schemadrift.sql import SqlError, run_sql_readonly

GRADING_ROW_LIMIT = 100_000
GRADING_TIMEOUT_S = 10.0

_REL_TOL = 1e-6
# Guards against float noise around zero (e.g. 1e-13 left over from a SUM of reals);
# no answer in the generated data is that small.
_ABS_TOL = 1e-9
_SCHEMA_ERRORS = ("no such column", "no such table")

Rows = list[list[Any]]


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _cells_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if _is_number(a) and _is_number(b):
        return math.isclose(float(a), float(b), rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    if _is_number(a) or _is_number(b) or isinstance(a, str) or isinstance(b, str):
        return False
    return bool(a == b)


def _rows_equal(a: list[Any], b: list[Any]) -> bool:
    return len(a) == len(b) and all(_cells_equal(x, y) for x, y in zip(a, b, strict=True))


_SIGNIFICANT_DIGITS = 9


def _canonical_cell(cell: Any) -> tuple[int, Any]:
    """A totally ordered key: NULL, then numbers (9 significant digits), text, anything else."""
    if cell is None:
        return (0, 0)
    if _is_number(cell):
        return (1, float(f"{float(cell):.{_SIGNIFICANT_DIGITS}g}"))
    if isinstance(cell, str):
        return (2, cell.strip().casefold())
    return (3, repr(cell))


def _canonical_row(row: list[Any]) -> tuple[tuple[int, Any], ...]:
    return tuple(_canonical_cell(cell) for cell in row)


def _multiset_equal(a: Rows, b: Rows) -> bool:
    """Sort both sides by canonical row key, then compare pairwise with the tolerance.

    Sorting makes the outcome independent of either side's row order.
    """
    return all(
        _rows_equal(x, y)
        for x, y in zip(sorted(a, key=_canonical_row), sorted(b, key=_canonical_row), strict=True)
    )


def results_equal(a: Rows, b: Rows, *, ordered: bool) -> bool:
    """Compare two result sets under the grading rules.

    Numbers match within a relative error of 1e-6, text matches after strip and
    casefold, NULL matches only NULL, and a number never matches text.

    Parameters
    ----------
    a, b
        Result rows.
    ordered
        Compare row by row in order; otherwise sort both sides by a canonical key
        (numbers rounded to 9 significant digits, text stripped and casefolded) and
        compare row by row, so the result never depends on either side's row order.

    Returns
    -------
    bool
        Whether the two results are equal.
    """
    if len(a) != len(b):
        return False
    if ordered:
        return all(_rows_equal(x, y) for x, y in zip(a, b, strict=True))
    return _multiset_equal(a, b)


def is_empty_result(rows: Rows) -> bool:
    """True for a result with no rows or with nothing but NULLs."""
    return not rows or all(cell is None for row in rows for cell in row)


def run_for_grading(db_path: Path, sql: str) -> Rows:
    """Run `sql` with the grading limits; a result past the row limit is an error.

    Raises
    ------
    SqlError
        On any execution error, a timeout, or more than `GRADING_ROW_LIMIT` rows.
    """
    result = run_sql_readonly(
        db_path, sql, row_limit=GRADING_ROW_LIMIT, timeout_s=GRADING_TIMEOUT_S
    )
    if result.truncated:
        raise SqlError(f"result has more than {GRADING_ROW_LIMIT} rows")
    return result.rows


def _is_schema_error(error: SqlError) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _SCHEMA_ERRORS)


def _is_stale(
    stream: Stream, question: Question, sql: str, epoch: int, current_error: SqlError | None
) -> bool:
    previous = epoch - 1
    expected_previous = question.expected_by_epoch[previous]
    try:
        previous_rows = run_for_grading(stream.db_path(previous), sql)
    except SqlError:
        return False
    if current_error is not None and _is_schema_error(current_error):
        return True
    return (
        results_equal(previous_rows, expected_previous, ordered=question.ordered)
        and not results_equal(
            expected_previous, question.expected_by_epoch[epoch], ordered=question.ordered
        )
        and not is_empty_result(expected_previous)
    )


def grade_submission(stream: Stream, question: Question, sql: str | None) -> Grade:
    """Grade a submitted SQL statement in a fixed order.

    `no_submission` when nothing was submitted; `correct` when the current epoch's
    full result matches; `stale` when the SQL answers the question as the previous
    epoch's rules would (or only fails because a column/table moved); otherwise
    `sql_error` for execution errors and `wrong_result` for the rest.

    Parameters
    ----------
    stream
        The stream whose epoch databases the SQL runs against.
    question
        The question being answered.
    sql
        The submitted SQL, or None when the agent submitted nothing.

    Returns
    -------
    Grade
        Score 1.0 for `correct`, 0.0 otherwise.
    """
    if sql is None:
        return Grade(category="no_submission", score=0.0)
    epoch = stream.epoch_of(question.session)
    current_error: SqlError | None = None
    try:
        rows = run_for_grading(stream.db_path(epoch), sql)
    except SqlError as err:
        current_error = err
    else:
        if results_equal(rows, question.expected_by_epoch[epoch], ordered=question.ordered):
            return Grade(category="correct", score=1.0)
    if epoch > 1 and _is_stale(stream, question, sql, epoch, current_error):
        return Grade(category="stale", score=0.0)
    if current_error is not None:
        return Grade(category="sql_error", score=0.0)
    return Grade(category="wrong_result", score=0.0)
