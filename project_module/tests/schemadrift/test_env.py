import sqlite3
from pathlib import Path
from typing import Any

import pytest
from project_module.contracts import Grade, TaskQuestion
from project_module.schemadrift.env import SchemaDriftEnv
from project_module.schemadrift.models import Question, Stream


def _make_db(path: Path, rows: list[tuple[Any, ...]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, note TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _stream(tmp_path: Path) -> Stream:
    """Epoch 1 holds three rows and epoch 2 five, so the two databases are distinguishable."""
    _make_db(tmp_path / "epoch_1.sqlite", [(1, "a"), (2, None), (3, "c")])
    _make_db(tmp_path / "epoch_2.sqlite", [(i, f"n{i}") for i in range(1, 6)])
    question = Question(
        qid="s05q1",
        session=5,
        text="How many rows are there?",
        ordered=False,
        fact_ids=[],
        drift_dependent=True,
        injected_target=False,
        gold_sql_by_epoch={1: "SELECT COUNT(*) FROM t", 2: "SELECT COUNT(*) FROM t"},
        expected_by_epoch={1: [[3]], 2: [[5]]},
    )
    return Stream(
        stream_id="handmade",
        seed=0,
        root=tmp_path,
        sessions=8,
        questions_per_session=1,
        questions=[question],
        facts=[],
        injections=[],
    )


def _env(tmp_path: Path, row_limit: int = 50) -> SchemaDriftEnv:
    return SchemaDriftEnv(_stream(tmp_path), row_limit=row_limit, timeout_s=5.0, max_queries=8)


def test_env_run_query_switches_epoch_db(tmp_path: Path) -> None:
    env = _env(tmp_path)

    before = env.run_query(4, "SELECT COUNT(*) AS n FROM t")
    after = env.run_query(5, "SELECT COUNT(*) AS n FROM t")

    assert (before, after) == ("n\n3", "n\n5")


def test_env_run_query_renders_rows_nulls_and_truncation(tmp_path: Path) -> None:
    env = _env(tmp_path, row_limit=2)

    text = env.run_query(1, "SELECT id, note FROM t ORDER BY id")

    assert text == "id\tnote\n1\ta\n2\tNULL\n... (truncated to 2 rows)"


def test_env_run_query_marks_empty_results(tmp_path: Path) -> None:
    env = _env(tmp_path)

    text = env.run_query(1, "SELECT id FROM t WHERE id > 100")

    assert text == "id\n(0 rows)"


@pytest.mark.parametrize(
    "sql",
    ["SELECT missing FROM t", "DELETE FROM t", "ATTACH DATABASE ':memory:' AS x"],
    ids=["unknown-column", "write", "attach"],
)
def test_env_run_query_returns_errors_as_text(tmp_path: Path, sql: str) -> None:
    env = _env(tmp_path)

    text = env.run_query(1, sql)

    assert text.startswith("ERROR: ")


def test_env_run_query_rejects_session_outside_stream(tmp_path: Path) -> None:
    env = _env(tmp_path)

    with pytest.raises(ValueError, match="outside"):
        env.run_query(9, "SELECT 1")


def test_env_serves_questions_without_answers(tmp_path: Path) -> None:
    env = _env(tmp_path)

    questions = env.questions_for(5)

    assert questions == [
        TaskQuestion(
            qid="s05q1",
            session=5,
            text="How many rows are there?",
            drift_dependent=True,
            injected_target=False,
        )
    ]
    assert env.questions_for(1) == []


def test_env_grades_submissions_against_the_current_epoch(tmp_path: Path) -> None:
    env = _env(tmp_path)
    question = env.questions_for(5)[0]

    assert env.grade(question, "SELECT COUNT(*) FROM t", queries_used=3) == Grade(
        category="correct", score=1.0
    )
    assert env.grade(question, None, queries_used=8).category == "no_submission"
    assert env.feedback(question, Grade(category="correct", score=1.0)) is None


def test_env_submit_tool_takes_one_sql_argument(tmp_path: Path) -> None:
    env = _env(tmp_path)

    tool = env.submit_tool()

    assert env.submit_param == "sql"
    assert tool["function"]["name"] == "submit_sql"
    assert tool["function"]["parameters"]["required"] == [env.submit_param]
    assert (env.env_id, env.sessions, env.max_queries) == ("handmade", 8, 8)
