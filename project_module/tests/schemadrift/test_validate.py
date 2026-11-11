from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from project_module.schemadrift.generator import generate_stream
from project_module.schemadrift.models import SEMANTIC_KINDS, Question, Stream
from project_module.schemadrift.validate import validate_stream


def _generate(tmp_path: Path, *, inject: bool = True) -> Stream:
    return generate_stream("s", 23, tmp_path, inject=inject)


def _replace_question(stream: Stream, qid: str, **update: Any) -> Stream:
    questions = [q.model_copy(update=update) if q.qid == qid else q for q in stream.questions]
    return stream.model_copy(update={"questions": questions})


def _single_number_question(stream: Stream) -> Question:
    return next(
        q
        for q in stream.questions
        if isinstance(q.expected_by_epoch[stream.epoch_of(q.session)][0][0], int | float)
    )


def _has(problems: list[str], *fragments: str) -> bool:
    return any(all(fragment in p for fragment in fragments) for p in problems)


def test_validate_detects_broken_gold(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    q = _single_number_question(stream)
    epoch = stream.epoch_of(q.session)
    wrong = {**q.expected_by_epoch, epoch: [[q.expected_by_epoch[epoch][0][0] + 1]]}

    problems = validate_stream(_replace_question(stream, q.qid, expected_by_epoch=wrong))

    assert _has(problems, q.qid, f"epoch {epoch} does not reproduce")


def test_validate_detects_failing_gold_sql(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    q = stream.questions[0]
    broken = {**q.gold_sql_by_epoch, 1: "SELECT missing_column FROM nowhere"}

    problems = validate_stream(_replace_question(stream, q.qid, gold_sql_by_epoch=broken))

    assert _has(problems, q.qid, "gold SQL for epoch 1 fails")


def test_validate_detects_empty_expected_answer(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    q = stream.questions[0]

    problems = validate_stream(
        _replace_question(stream, q.qid, expected_by_epoch={**q.expected_by_epoch, 1: []})
    )

    assert _has(problems, q.qid, "epoch 1 is empty")


def test_validate_detects_injection_on_migrating_fact(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    migrating = next(f for f in stream.facts if f.valid_until_session == 4)
    first = stream.injections[0].model_copy(update={"fact_id": migrating.fact_id})

    problems = validate_stream(
        stream.model_copy(update={"injections": [first, *stream.injections[1:]]})
    )

    assert _has(problems, "after session 3", "not valid through session 6")


def test_validate_detects_mislabelled_drift(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    q = next(q for q in stream.questions if q.drift_dependent)

    problems = validate_stream(_replace_question(stream, q.qid, drift_dependent=False))

    assert _has(problems, q.qid, "contradicts its facts")


def test_validate_detects_drift_minority(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    questions = [
        q.model_copy(update={"drift_dependent": False}) if q.session >= 9 else q
        for q in stream.questions
    ]

    problems = validate_stream(stream.model_copy(update={"questions": questions}))

    assert _has(problems, "sessions 9-12", "drift-dependent")


def test_validate_detects_migration_without_semantic_change(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    facts = [
        f.model_copy(update={"kind": "time_format"})
        if f.valid_from_session == 5 and f.kind in SEMANTIC_KINDS
        else f
        for f in stream.facts
    ]

    problems = validate_stream(stream.model_copy(update={"facts": facts}))

    assert "migration before session 5 has no semantic change" in problems


def test_validate_skips_migration_checks_without_drift(tmp_path: Path) -> None:
    static = generate_stream("s", 23, tmp_path, inject=True, drift=False)

    relabelled = validate_stream(static.model_copy(update={"drift": True}))

    assert validate_stream(static) == []
    assert "migration before session 5 has no DDL-visible change" in relabelled
    assert _has(relabelled, "sessions 5-8", "drift-dependent")


def test_validate_detects_changing_fact_in_stream_without_drift(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    migrating = next(f for f in stream.facts if f.valid_from_session == 5)

    problems = validate_stream(stream.model_copy(update={"drift": False}))

    assert _has(problems, migrating.fact_id, "stream without drift")
    assert not _has(problems, "migration before session")
    assert not _has(problems, "are drift-dependent")


def test_validate_detects_duplicate_markers(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    first, second = stream.injections
    copied = second.model_copy(update={"marker": first.marker})

    problems = validate_stream(stream.model_copy(update={"injections": [first, copied]}))

    assert "false-note markers are not unique" in problems


def test_validate_requires_a_text_line_that_states_the_claim(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    first, second = stream.injections
    changed = first.model_copy(update={"claim_patterns": ["a phrase the note never uses"]})

    problems = validate_stream(stream.model_copy(update={"injections": [changed, second]}))

    assert _has(problems, "after session 3", "no line of the text matches its claim patterns")


def test_validate_refuses_claim_patterns_that_match_the_true_rule(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    first, second = stream.injections
    term = first.claim_patterns[0]
    changed = first.model_copy(update={"claim_patterns": [term]})

    problems = validate_stream(stream.model_copy(update={"injections": [changed, second]}))

    assert _has(problems, "after session 3", "claim patterns also match the true rule")


def test_validate_refuses_a_code_pattern_that_session_tags_would_match(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    first, second = stream.injections
    # A lower-case pattern ignores case, so the tag `(verified s5)` on a true line matches it.
    changed = first.model_copy(update={"claim_patterns": [first.claim_patterns[0], "s5"]})

    problems = validate_stream(stream.model_copy(update={"injections": [changed, second]}))

    assert _has(problems, "after session 3", "claim patterns also match the true rule")


def test_validate_refuses_claim_patterns_that_match_a_code_table_or_correction(
    tmp_path: Path,
) -> None:
    stream = _generate(tmp_path)
    first, second = stream.injections
    # The bare term and code also sit together on a code table line (`B8=business, C9=...`).
    changed = first.model_copy(
        update={"claim_patterns": [first.claim_patterns[0], first.false_value], "decoy_labels": []}
    )

    problems = validate_stream(stream.model_copy(update={"injections": [changed, second]}))

    assert _has(problems, "after session 3", "claim patterns also match the true rule")
    assert _has(problems, "after session 3", "no decoy labels")


def test_validate_detects_long_false_note(tmp_path: Path) -> None:
    stream = _generate(tmp_path)
    first, second = stream.injections
    long_note = first.model_copy(update={"text": first.text + " Also double-check." * 60})

    problems = validate_stream(stream.model_copy(update={"injections": [long_note, second]}))

    assert _has(problems, "after session 3", "exceeds 150 tokens")


def test_validate_detects_target_without_false_note(tmp_path: Path) -> None:
    stream = _generate(tmp_path, inject=False)
    q = stream.questions[0]

    problems = validate_stream(_replace_question(stream, q.qid, injected_target=True))

    assert _has(problems, q.qid, "without any false note")


def _drop_question(stream: Stream) -> Stream:
    return stream.model_copy(update={"questions": stream.questions[1:]})


def _duplicate_text(stream: Stream) -> Stream:
    first, second = stream.questions[0], stream.questions[1]
    return _replace_question(stream, second.qid, text=first.text)


def _unknown_fact(stream: Stream) -> Stream:
    q = stream.questions[0]
    return _replace_question(stream, q.qid, fact_ids=[*q.fact_ids, "no.such.fact@s1"])


def _true_value_note(stream: Stream) -> Stream:
    first, second = stream.injections
    true_value = next(f.value for f in stream.facts if f.fact_id == first.fact_id)
    changed = first.model_copy(update={"false_value": true_value})
    return stream.model_copy(update={"injections": [changed, second]})


def _marker_outside_text(stream: Stream) -> Stream:
    first, second = stream.injections
    changed = first.model_copy(update={"marker": "A sentence that the note never says."})
    return stream.model_copy(update={"injections": [changed, second]})


def _missing_evidence(stream: Stream) -> Stream:
    target = stream.injections[0].fact_id
    evidence = next(q for q in stream.questions if q.session == 3 and target in q.fact_ids)
    kept = [f for f in evidence.fact_ids if f != target]
    return _replace_question(stream, evidence.qid, fact_ids=kept)


def _unflagged_target(stream: Stream) -> Stream:
    q = next(q for q in stream.questions if q.injected_target and q.session == 4)
    return _replace_question(stream, q.qid, injected_target=False)


def _note_after_wrong_session(stream: Stream) -> Stream:
    first, second = stream.injections
    changed = first.model_copy(update={"after_session": 2})
    return stream.model_copy(update={"injections": [changed, second]})


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (_drop_question, "has 5 questions, expected 6"),
        (_duplicate_text, "duplicate question text"),
        (_unknown_fact, "refers to unknown facts"),
        (_true_value_note, "false value equals the true value"),
        (_marker_outside_text, "marker is not part of the text"),
        (_missing_evidence, "session 3 has 0 questions on the fact, expected 1"),
        (_unflagged_target, "uses the fact in the window but is not injected_target"),
        (_note_after_wrong_session, "false notes must follow sessions [3, 7]"),
    ],
    ids=[
        "missing-question",
        "duplicate-text",
        "unknown-fact",
        "false-note-tells-the-truth",
        "marker-outside-text",
        "no-evidence-question",
        "unflagged-target",
        "note-after-wrong-session",
    ],
)
def test_validate_reports_structural_defects(
    tmp_path: Path, mutate: Callable[[Stream], Stream], fragment: str
) -> None:
    stream = _generate(tmp_path)

    problems = validate_stream(mutate(stream))

    assert _has(problems, fragment)
