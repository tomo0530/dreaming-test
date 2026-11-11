import hashlib
import re
import sqlite3
from pathlib import Path

import pytest
from project_module.schemadrift.generator import STREAM_FILE, generate_stream, load_stream
from project_module.schemadrift.grader import grade_submission
from project_module.schemadrift.models import Stream
from project_module.schemadrift.validate import validate_stream

VALIDATION_SEEDS = [11, 23, 37, 41, 58]
NO_DRIFT_SEEDS = range(4501, 4521)
# SHA-256 of seed 4521 (inject=True) as generated before streams without drift existed.
PINNED_SEED = 4521
PINNED_STREAM_SHA256 = "e9b097005819c1572b807a7d736633e442b027fe3254bcecf11469737bf6eb5a"
PINNED_DB_SHA256 = {
    1: "8e014710f43149a1cf450b3d2e8ed6c261e0e47d5d2a40f5b53242f957de4005",
    2: "e5cbf4fc412531560b91642b1a14e286789ebae3912d5741503e8997bb9c95c0",
    3: "5c1b82c159a56ab58a8d8e01583dee515271467f1be528aa0672dceeb2122ee3",
}


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_names(db_path: Path) -> dict[str, list[str]]:
    conn = sqlite3.connect(db_path)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master ORDER BY name")]
        return {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
    finally:
        conn.close()


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
        term, assignment = note.claim_patterns
        assert term in fact.subject
        assert assignment.endswith(f" = '{note.false_value}'")
        assert note.decoy_labels
        assert all(term not in label.casefold() for label in note.decoy_labels)
        claim_lines = [line for line in note.text.splitlines() if note.states_claim(line)]
        assert len(claim_lines) == 1  # the rule line; the marker line counts by the marker
        assert assignment in claim_lines[0]
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


def test_drift_default_is_unchanged(tmp_path: Path) -> None:
    stream = generate_stream("s", PINNED_SEED, tmp_path, inject=True)

    assert stream.drift
    assert _sha256(tmp_path / STREAM_FILE) == PINNED_STREAM_SHA256
    assert {e: _sha256(stream.db_path(e)) for e in PINNED_DB_SHA256} == PINNED_DB_SHA256


def test_no_drift_stream_has_static_facts(tmp_path: Path) -> None:
    stream = generate_stream("s", 4501, tmp_path, inject=True, drift=False)

    assert not stream.drift
    assert all(f.valid_from_session == 1 and f.valid_until_session is None for f in stream.facts)
    assert not any(q.drift_dependent for q in stream.questions)
    assert load_stream(tmp_path) == stream


def test_no_drift_keeps_names_of_same_seed(tmp_path: Path) -> None:
    drifting = generate_stream("s", 4502, tmp_path / "drift", inject=True)
    static = generate_stream("s", 4502, tmp_path / "static", inject=True, drift=False)
    epoch_one = _schema_names(drifting.db_path(1))

    assert _dump(static.db_path(1)) == _dump(drifting.db_path(1))
    assert all(_schema_names(static.db_path(e)) == epoch_one for e in (2, 3))
    assert {(f.fact_id, f.subject, f.value) for f in static.facts} == {
        (f.fact_id, f.subject, f.value) for f in drifting.facts if f.valid_at(1)
    }


def test_no_drift_stream_still_adds_rows(tmp_path: Path) -> None:
    stream = generate_stream("s", 4503, tmp_path, inject=False, drift=False)
    sizes = [len(_dump(stream.db_path(e))) for e in (1, 2, 3)]

    assert sizes == sorted(sizes)
    assert len(set(sizes)) == 3


@pytest.mark.parametrize("seed", NO_DRIFT_SEEDS)
def test_no_drift_streams_validate_for_many_seeds(tmp_path: Path, seed: int) -> None:
    stream = generate_stream(f"s{seed}", seed, tmp_path, inject=True, drift=False)

    assert validate_stream(stream) == []


@pytest.mark.slow
@pytest.mark.parametrize("drift", [True, False], ids=["drift", "no-drift"])
def test_many_seeds_pass_validation(tmp_path: Path, drift: bool) -> None:
    failing = {
        seed: problems
        for seed in range(100)
        if (
            problems := validate_stream(
                generate_stream("s", seed, tmp_path / str(seed), inject=True, drift=drift)
            )
        )
    }

    assert failing == {}
