import sqlite3
from pathlib import Path
from typing import Any

import pytest
from project_module.contracts import Grade, GradeCategory, TaskQuestion
from project_module.schemadrift.env import FEEDBACK_MAX_CHARS, FEEDBACK_MAX_ROWS, SchemaDriftEnv
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


def test_env_submit_tool_takes_one_sql_argument(tmp_path: Path) -> None:
    env = _env(tmp_path)

    tool = env.submit_tool()

    assert env.submit_param == "sql"
    assert tool["function"]["name"] == "submit_sql"
    assert tool["function"]["parameters"]["required"] == [env.submit_param]
    assert (env.env_id, env.sessions, env.max_queries) == ("handmade", 8, 8)


# --- graded feedback ---------------------------------------------------------------------------

_CORRECT = Grade(category="correct", score=1.0)
_TRUNCATION_MARK = "[truncated]"


def _question(qid: str, session: int, expected_by_epoch: dict[int, list[list[Any]]]) -> Question:
    return Question(
        qid=qid,
        session=session,
        text=f"Text of {qid}",
        ordered=False,
        fact_ids=[],
        drift_dependent=False,
        injected_target=False,
        gold_sql_by_epoch={epoch: "SELECT 1" for epoch in expected_by_epoch},
        expected_by_epoch=expected_by_epoch,
    )


def _feedback_env(
    tmp_path: Path, questions: list[Question], *, feedback: bool = True
) -> SchemaDriftEnv:
    """An env over hand-made questions; feedback never touches the epoch databases."""
    stream = Stream(
        stream_id="handmade",
        seed=0,
        root=tmp_path,
        sessions=8,
        questions_per_session=3,
        questions=questions,
        facts=[],
        injections=[],
    )
    return SchemaDriftEnv(stream, row_limit=50, timeout_s=5.0, max_queries=8, feedback=feedback)


def _question_seven(expected_by_epoch: dict[int, list[list[Any]]]) -> list[Question]:
    """Three questions in each of sessions 1 and 2, then the one under test in session 5."""
    earlier = [
        _question(f"s{session:02d}q{index}", session, {1: [[0]], 2: [[0]]})
        for session in (1, 2)
        for index in (1, 2, 3)
    ]
    return [*earlier, _question("s05q1", 5, expected_by_epoch)]


def _expected_block(text: str, label: str) -> str:
    return text.split(f"{label}\n", 1)[1]


def test_feedback_is_off_by_default(tmp_path: Path) -> None:
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: [[5]]}), feedback=False)

    result = sut.feedback(sut.questions_for(5)[0], _CORRECT)

    assert result is None


def test_feedback_correct_line(tmp_path: Path) -> None:
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: [[5]]}))

    result = sut.feedback(sut.questions_for(5)[0], _CORRECT)

    assert result == "Question 7: CORRECT!"


def test_feedback_numbers_questions_across_sessions(tmp_path: Path) -> None:
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: [[5]]}))

    first = sut.feedback(sut.questions_for(1)[0], _CORRECT)
    last_of_session_two = sut.feedback(sut.questions_for(2)[2], _CORRECT)

    assert (first, last_of_session_two) == ("Question 1: CORRECT!", "Question 6: CORRECT!")


@pytest.mark.parametrize("category", ["stale", "sql_error", "wrong_result"])
def test_feedback_incorrect_lists_current_epoch_expected_rows(
    tmp_path: Path, category: GradeCategory
) -> None:
    expected: dict[int, list[list[Any]]] = {
        1: [[3]],
        2: [[1, "a"], [2, None], ["x\ty", 2.5]],
    }
    sut = _feedback_env(tmp_path, _question_seven(expected))

    result = sut.feedback(sut.questions_for(5)[0], Grade(category=category, score=0.0))

    assert result == "Question 7: INCORRECT.\nCorrect answer:\n1 | a\n2 | NULL\nx\\ty | 2.5"


def test_feedback_no_submission_wording(tmp_path: Path) -> None:
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: [[5]]}))

    result = sut.feedback(sut.questions_for(5)[0], Grade(category="no_submission", score=0.0))

    assert result == (
        "Question 7: No answer was submitted within the limits — treated as incorrect.\n"
        "The correct answer was:\n5"
    )


def test_feedback_marks_an_empty_expected_result(tmp_path: Path) -> None:
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: []}))

    result = sut.feedback(sut.questions_for(5)[0], Grade(category="wrong_result", score=0.0))

    assert result == "Question 7: INCORRECT.\nCorrect answer:\n(0 rows)"


def test_feedback_keeps_at_most_twenty_rows(tmp_path: Path) -> None:
    rows = [[f"r{i}", i] for i in range(1, 26)]
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: rows}))

    result = sut.feedback(sut.questions_for(5)[0], Grade(category="wrong_result", score=0.0))

    assert result is not None and FEEDBACK_MAX_ROWS == 20
    assert _expected_block(result, "Correct answer:").splitlines() == [
        *(f"r{i} | {i}" for i in range(1, 21)),
        _TRUNCATION_MARK,
    ]


def test_feedback_drops_whole_rows_past_the_character_limit(tmp_path: Path) -> None:
    rows = [["x" * 300] for _ in range(10)]
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: rows}))

    result = sut.feedback(sut.questions_for(5)[0], Grade(category="wrong_result", score=0.0))

    assert result is not None
    block = _expected_block(result, "Correct answer:")
    *kept, mark = block.splitlines()
    assert mark == _TRUNCATION_MARK
    assert 0 < len(kept) < 10 and all(line == "x" * 300 for line in kept)
    assert len(block) <= FEEDBACK_MAX_CHARS


def test_feedback_cuts_a_single_oversized_row(tmp_path: Path) -> None:
    sut = _feedback_env(tmp_path, _question_seven({1: [[3]], 2: [["y" * 5000]]}))

    result = sut.feedback(sut.questions_for(5)[0], Grade(category="wrong_result", score=0.0))

    assert result is not None
    block = _expected_block(result, "Correct answer:")
    assert block.endswith(f"y\n{_TRUNCATION_MARK}")
    assert len(block) <= FEEDBACK_MAX_CHARS
