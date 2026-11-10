import re
import sqlite3
from pathlib import Path

import pytest
from project_module.schemadrift.generator import STREAM_FILE, generate_stream, load_stream
from project_module.schemadrift.grader import grade_submission
from project_module.schemadrift.models import Stream
from project_module.schemadrift.validate import validate_stream

VALIDATION_SEEDS = [11, 23, 37, 41, 58]


def _dump(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def _column_names(stream: Stream) -> set[str]:
    names: set[str] = set()
    for epoch in range(1, stream.epoch_of(stream.sessions) + 1):
        conn = sqlite3.connect(stream.db_path(epoch))
        try:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
            for table in tables:
                names |= {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()
    return names


def test_generate_stream_is_deterministic(tmp_path: Path) -> None:
    first = generate_stream("s", 11, tmp_path / "a", inject=True)
    second = generate_stream("s", 11, tmp_path / "b", inject=True)

    assert (tmp_path / "a" / STREAM_FILE).read_bytes() == (
        tmp_path / "b" / STREAM_FILE
    ).read_bytes()
    for epoch in (1, 2, 3):
        assert _dump(first.db_path(epoch)) == _dump(second.db_path(epoch))


def test_different_seeds_give_different_streams(tmp_path: Path) -> None:
    generate_stream("s", 11, tmp_path / "a", inject=True)
    generate_stream("s", 12, tmp_path / "b", inject=True)

    assert (tmp_path / "a" / STREAM_FILE).read_bytes() != (
        tmp_path / "b" / STREAM_FILE
    ).read_bytes()


@pytest.mark.parametrize("seed", VALIDATION_SEEDS)
def test_generated_streams_pass_validation(tmp_path: Path, seed: int) -> None:
    stream = generate_stream(f"s{seed}", seed, tmp_path, inject=True)

    assert validate_stream(stream) == []


def test_generated_stream_has_the_designed_shape(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=True)

    assert len(stream.questions) == 72
    assert [n.after_session for n in stream.injections] == [3, 7]
    assert {stream.db_path(e).name for e in (1, 2, 3)} == {
        p.name for p in tmp_path.glob("epoch_*.sqlite")
    }


def test_false_notes_are_declarative_and_carry_their_claim(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=True)

    for note in stream.injections:
        fact = stream.fact(note.fact_id)
        lowered = note.text.lower()
        for imperative in ("whenever", "filter on", "always ", "make sure", "use "):
            assert imperative not in lowered, imperative
        assert note.claim_patterns == [note.claim_patterns[0], note.false_value]
        assert note.claim_patterns[0] in fact.subject
        claim_lines = [line for line in note.text.splitlines() if note.states_claim(line)]
        assert len(claim_lines) == 2  # the rule line and the marker line
        assert not note.states_claim(note.text.replace(note.false_value, fact.value))


def test_load_stream_round_trips(tmp_path: Path) -> None:
    generated = generate_stream("s", 23, tmp_path, inject=True)

    loaded = load_stream(tmp_path)

    assert loaded == generated


def test_stream_file_does_not_depend_on_location(tmp_path: Path) -> None:
    generate_stream("s", 23, tmp_path, inject=False)

    assert str(tmp_path) not in (tmp_path / STREAM_FILE).read_text()


def test_inject_false_produces_no_injections(tmp_path: Path) -> None:
    plain = generate_stream("s", 37, tmp_path / "plain", inject=False)
    injected = generate_stream("s", 37, tmp_path / "injected", inject=True)

    assert plain.injections == []
    assert not any(q.injected_target for q in plain.questions)
    assert [q.text for q in plain.questions] == [q.text for q in injected.questions]
    assert validate_stream(plain) == []


def test_question_texts_name_no_column(tmp_path: Path) -> None:
    stream = generate_stream("s", 41, tmp_path, inject=True)
    columns = _column_names(stream)

    leaks = {
        (q.qid, column)
        for q in stream.questions
        for column in columns
        if re.search(rf"\b{re.escape(column)}\b", q.text, flags=re.IGNORECASE)
    }

    assert leaks == set()


def test_previous_epoch_gold_is_graded_stale_on_drift_questions(tmp_path: Path) -> None:
    stream = generate_stream("s", 58, tmp_path, inject=False)
    drift = [q for q in stream.questions if q.drift_dependent]

    grades = {
        q.qid: (
            grade_submission(stream, q, q.gold_sql_by_epoch[stream.epoch_of(q.session)]).category,
            grade_submission(
                stream, q, q.gold_sql_by_epoch[stream.epoch_of(q.session) - 1]
            ).category,
        )
        for q in drift
    }

    assert drift
    assert set(grades.values()) == {("correct", "stale")}


def test_shorter_stream_covers_only_its_epochs(tmp_path: Path) -> None:
    stream = generate_stream("s", 11, tmp_path, sessions=4, questions_per_session=5, inject=False)

    assert sorted(p.name for p in tmp_path.glob("epoch_*.sqlite")) == ["epoch_1.sqlite"]
    assert len(stream.questions) == 20
    assert validate_stream(stream) == []


@pytest.mark.parametrize(
    ("sessions", "questions_per_session", "inject", "message"),
    [
        (13, 6, False, "sessions"),
        (0, 6, False, "sessions"),
        (12, 2, False, "questions_per_session"),
        (9, 6, True, "injection"),
    ],
    ids=["too-many-sessions", "no-sessions", "too-few-questions", "injection-without-window"],
)
def test_generate_stream_rejects_unsupported_shapes(
    tmp_path: Path, sessions: int, questions_per_session: int, inject: bool, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        generate_stream(
            "s",
            1,
            tmp_path,
            sessions=sessions,
            questions_per_session=questions_per_session,
            inject=inject,
        )


@pytest.mark.slow
def test_many_seeds_pass_validation(tmp_path: Path) -> None:
    failing = {
        seed: problems
        for seed in range(100)
        if (
            problems := validate_stream(
                generate_stream("s", seed, tmp_path / str(seed), inject=True)
            )
        )
    }

    assert failing == {}
