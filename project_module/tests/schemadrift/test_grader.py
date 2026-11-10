import sqlite3
from pathlib import Path
from typing import Any

import pytest
from project_module.schemadrift.grader import grade_submission, results_equal
from project_module.schemadrift.models import Question, Stream


def _make_db(path: Path, ddl: str, rows: list[tuple[Any, ...]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    placeholders = ", ".join("?" for _ in rows[0])
    conn.executemany(f"INSERT INTO sales VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()


def _question(
    qid: str,
    session: int,
    expected: dict[int, list[list[Any]]],
    *,
    ordered: bool = False,
) -> Question:
    return Question(
        qid=qid,
        session=session,
        text="t",
        ordered=ordered,
        fact_ids=[],
        drift_dependent=session > 4,
        injected_target=False,
        gold_sql_by_epoch={epoch: "SELECT 1" for epoch in expected},
        expected_by_epoch=expected,
    )


def _drift_stream(tmp_path: Path) -> Stream:
    """Epoch 1 stores cents in `amt` and has `region`; epoch 2 stores dollars and renames it `zone`."""
    _make_db(
        tmp_path / "epoch_1.sqlite",
        "CREATE TABLE sales (id INTEGER, amt INTEGER, region TEXT)",
        [(1, 1000, "N"), (2, 2500, "S"), (3, 4000, "N")],
    )
    _make_db(
        tmp_path / "epoch_2.sqlite",
        "CREATE TABLE sales (id INTEGER, amt REAL, zone TEXT)",
        [(1, 10.0, "N"), (2, 25.0, "S"), (3, 40.0, "N"), (4, 15.0, "N")],
    )
    questions = [
        _question("north-total", 5, {1: [[50.0]], 2: [[65.0]]}),
        _question("grand-total", 5, {1: [[75.0]], 2: [[90.0]]}),
        _question("early-count", 5, {1: [[3]], 2: [[3]]}),
        _question("fourth-sale", 5, {1: [], 2: [[1]]}),
        _question("epoch-one-total", 2, {1: [[75.0]]}),
    ]
    return Stream(
        stream_id="handmade",
        seed=0,
        root=tmp_path,
        sessions=8,
        questions_per_session=1,
        questions=questions,
        facts=[],
        injections=[],
    )


def _by_id(stream: Stream, qid: str) -> Question:
    return next(q for q in stream.questions if q.qid == qid)


@pytest.mark.parametrize(
    ("qid", "sql", "category"),
    [
        ("north-total", "SELECT SUM(amt) FROM sales WHERE zone = 'N'", "correct"),
        ("grand-total", "SELECT SUM(amt) / 100.0 FROM sales", "stale"),
        ("north-total", "SELECT SUM(amt) / 100.0 FROM sales WHERE region = 'N'", "stale"),
        ("north-total", "SELECT SUM(amt) FROM sales WHERE region = 'N'", "stale"),
        ("north-total", "SELECT SUM(amount) FROM sales", "sql_error"),
        ("north-total", "SELECT 1", "wrong_result"),
        ("north-total", None, "no_submission"),
        ("early-count", "SELECT COUNT(*) FROM sales WHERE amt >= 1000", "wrong_result"),
        ("fourth-sale", "SELECT id FROM sales WHERE id = 4", "wrong_result"),
        ("epoch-one-total", "SELECT SUM(amt) FROM sales", "wrong_result"),
        (
            "north-total",
            "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r WHERE x < 100001) "
            "SELECT x FROM r",
            "sql_error",
        ),
    ],
    ids=[
        "current-gold-is-correct",
        "previous-epoch-gold-is-stale",
        "old-column-matching-old-answer-is-stale",
        "old-column-that-runs-on-previous-db-is-stale",
        "unknown-column-is-sql-error",
        "other-value-is-wrong-result",
        "no-sql-is-no-submission",
        "matching-old-answer-is-not-stale-when-answers-agree",
        "matching-old-answer-is-not-stale-when-old-answer-is-empty",
        "epoch-one-question-is-never-stale",
        "result-past-grading-row-limit-is-sql-error",
    ],
)
def test_grade_categories(tmp_path: Path, qid: str, sql: str | None, category: str) -> None:
    stream = _drift_stream(tmp_path)

    grade = grade_submission(stream, _by_id(stream, qid), sql)

    assert grade.category == category
    assert grade.score == (1.0 if category == "correct" else 0.0)


def test_grade_compares_full_result_not_truncated(tmp_path: Path) -> None:
    _make_db(
        tmp_path / "epoch_1.sqlite",
        "CREATE TABLE sales (id INTEGER, amt INTEGER)",
        [(i, i * 10) for i in range(120)],
    )
    expected = [[i] for i in range(120)]
    question = _question("all-ids", 1, {1: expected})
    stream = Stream(
        stream_id="handmade",
        seed=0,
        root=tmp_path,
        sessions=4,
        questions_per_session=1,
        questions=[question],
        facts=[],
        injections=[],
    )

    first_rows_only = grade_submission(stream, question, "SELECT id FROM sales LIMIT 50")
    full = grade_submission(stream, question, "SELECT id FROM sales")

    assert first_rows_only.category == "wrong_result"
    assert full.category == "correct"


@pytest.mark.parametrize(
    ("a", "b", "ordered", "equal"),
    [
        ([[1]], [[1.0000001]], False, True),
        ([[1.0]], [[1.00001]], False, False),
        ([[0.0]], [[1e-12]], False, True),
        ([["  North "]], [["north"]], False, True),
        ([["Straße"]], [["STRASSE"]], False, True),
        ([[None]], [[None]], False, True),
        ([[None]], [[0]], False, False),
        ([[None]], [[""]], False, False),
        ([[5]], [["5"]], False, False),
        ([[5]], [["5"]], True, False),
        ([[b"\x01"]], [[b"\x01"]], False, True),
        ([[b"\x01"]], [[b"\x02"]], True, False),
        ([[1], [2], [2]], [[2], [1], [2]], False, True),
        ([[1], [2], [2]], [[1], [1], [2]], False, False),
        ([["a", 1.0], ["b", 2.0]], [["b", 2.0000001], ["a", 1.0]], False, True),
        ([[1], [2]], [[2], [1]], True, False),
        ([[1], [2]], [[1], [2]], True, True),
        ([[1]], [[1], [1]], False, False),
        ([[1, 2]], [[1]], False, False),
        ([], [], False, True),
    ],
    ids=[
        "relative-error-within-tolerance",
        "relative-error-beyond-tolerance",
        "float-noise-around-zero",
        "strings-strip-and-casefold",
        "strings-unicode-casefold",
        "null-matches-null",
        "null-does-not-match-zero",
        "null-does-not-match-empty-string",
        "number-does-not-match-text",
        "number-does-not-match-text-ordered",
        "blobs-compare-exactly",
        "different-blobs",
        "unordered-is-multiset",
        "unordered-counts-duplicates",
        "unordered-with-float-tolerance",
        "ordered-respects-order",
        "ordered-equal",
        "different-row-counts",
        "different-row-widths",
        "both-empty",
    ],
)
def test_results_equal_rules(
    a: list[list[Any]], b: list[list[Any]], ordered: bool, equal: bool
) -> None:
    assert results_equal(a, b, ordered=ordered) is equal


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([[1.0000008], [1.0]], [[1.0], [1.0000016]]),
        ([[None, "x"], [3, "Y"], [2.5, None]], [[2.5, None], [3.0, "y "], [None, "X"]]),
        ([[1, "a"], [1, "b"], ["t", 2]], [["t", 2], [1, "b"], [1, "a"]]),
    ],
    ids=["reported-counterexample", "mixed-types-and-nulls", "mixed-column-types"],
)
def test_unordered_comparison_does_not_depend_on_row_order(
    a: list[list[Any]], b: list[list[Any]]
) -> None:
    pairs = [(x, y) for x in (a, a[::-1]) for y in (b, b[::-1])]
    outcomes = {results_equal(x, y, ordered=False) for x, y in pairs}
    outcomes |= {results_equal(y, x, ordered=False) for x, y in pairs}
    assert outcomes == {True}


def test_unordered_comparison_is_symmetric_on_a_mismatch() -> None:
    a, b = [[1.0], [2.0]], [[1.0], [2.1]]
    assert {results_equal(x, y, ordered=False) for x in (a, a[::-1]) for y in (b, b[::-1])} == {
        False
    }
