"""Soundness checks for a generated SchemaDrift-SQL stream.

The same function guards tests, stream generation scripts and pilot gate (1): a
stream is sound only when `validate_stream` returns no problems.
"""

from collections import Counter

from project_module.schemadrift.grader import is_empty_result, results_equal, run_for_grading
from project_module.schemadrift.models import (
    DDL_VISIBLE_KINDS,
    SEMANTIC_KINDS,
    SESSIONS_PER_EPOCH,
    Fact,
    InjectedNote,
    Question,
    Stream,
)
from project_module.schemadrift.sql import SqlError
from project_module.tokens import count_tokens

INJECTION_AFTER_SESSIONS = (3, 7)
# Questions on the false note's fact sit in the sessions right after the note.
INJECTION_WINDOW = 3
MAX_NOTE_TOKENS = 150


def epoch_start(epoch: int) -> int:
    """First session of `epoch`; a migration happens right before it (epoch > 1)."""
    return (epoch - 1) * SESSIONS_PER_EPOCH + 1


def _migration_starts(stream: Stream) -> range:
    return range(SESSIONS_PER_EPOCH + 1, stream.sessions + 1, SESSIONS_PER_EPOCH)


def _check_layout(stream: Stream) -> list[str]:
    problems: list[str] = []
    for label, values in (
        ("qid", [q.qid for q in stream.questions]),
        ("question text", [q.text for q in stream.questions]),
    ):
        duplicates = sorted(v for v, n in Counter(values).items() if n > 1)
        if duplicates:
            problems.append(f"duplicate {label}: {duplicates}")
    for session in range(1, stream.sessions + 1):
        questions = stream.questions_for(session)
        if len(questions) != stream.questions_per_session:
            problems.append(
                f"session {session} has {len(questions)} questions, "
                f"expected {stream.questions_per_session}"
            )
        if not any(len(q.fact_ids) >= 2 for q in questions):
            problems.append(f"session {session} has no question combining two or more rules")
    return problems


def _old_sql_still_answers(stream: Stream, question: Question, epoch: int) -> bool:
    try:
        rows = run_for_grading(stream.db_path(epoch), question.gold_sql_by_epoch[epoch - 1])
    except SqlError:
        return False
    return results_equal(rows, question.expected_by_epoch[epoch], ordered=question.ordered)


def _check_drift(stream: Stream, question: Question, epoch: int) -> list[str]:
    qid, ordered = question.qid, question.ordered
    still_answers = _old_sql_still_answers(stream, question, epoch)
    if not question.drift_dependent:
        if still_answers:
            return []
        return [
            f"{qid}: epoch {epoch - 1} gold SQL no longer answers it, yet it is not drift-dependent"
        ]
    problems = []
    current, previous = question.expected_by_epoch[epoch], question.expected_by_epoch[epoch - 1]
    if results_equal(current, previous, ordered=ordered):
        problems.append(
            f"{qid}: drift-dependent but its answer equals the epoch {epoch - 1} answer"
        )
    if still_answers:
        problems.append(
            f"{qid}: drift-dependent but the epoch {epoch - 1} gold SQL still answers it"
        )
    return problems


def _check_question(stream: Stream, question: Question, facts: dict[str, Fact]) -> list[str]:
    qid, epoch = question.qid, stream.epoch_of(question.session)
    epochs = set(range(1, epoch + 1))
    if set(question.gold_sql_by_epoch) != epochs or set(question.expected_by_epoch) != epochs:
        return [f"{qid}: gold SQL and expected answers must cover epochs {sorted(epochs)}"]
    problems: list[str] = []
    used = [facts[fid] for fid in question.fact_ids if fid in facts]
    if len(used) != len(question.fact_ids):
        problems.append(f"{qid}: refers to unknown facts")
    problems += [
        f"{qid}: fact {f.fact_id} is not valid in session {question.session}"
        for f in used
        if not f.valid_at(question.session)
    ]
    drift = epoch > 1 and any(f.valid_from_session == epoch_start(epoch) for f in used)
    if drift != question.drift_dependent:
        problems.append(f"{qid}: drift_dependent={question.drift_dependent} contradicts its facts")
    for e in sorted(epochs):
        expected = question.expected_by_epoch[e]
        if is_empty_result(expected):
            problems.append(f"{qid}: expected answer for epoch {e} is empty")
        try:
            rows = run_for_grading(stream.db_path(e), question.gold_sql_by_epoch[e])
        except SqlError as err:
            problems.append(f"{qid}: gold SQL for epoch {e} fails: {err}")
            continue
        if not results_equal(rows, expected, ordered=question.ordered):
            problems.append(f"{qid}: gold SQL for epoch {e} does not reproduce the expected answer")
    if epoch > 1:
        problems += _check_drift(stream, question, epoch)
    return problems


def _check_migrations(stream: Stream) -> list[str]:
    problems: list[str] = []
    for start in _migration_starts(stream):
        kinds = {f.kind for f in stream.facts if f.valid_from_session == start}
        if not kinds & DDL_VISIBLE_KINDS:
            problems.append(f"migration before session {start} has no DDL-visible change")
        if not kinds & SEMANTIC_KINDS:
            problems.append(f"migration before session {start} has no semantic change")
    return problems


def _check_drift_majority(stream: Stream) -> list[str]:
    problems: list[str] = []
    for start in _migration_starts(stream):
        block = [q for q in stream.questions if start <= q.session < start + SESSIONS_PER_EPOCH]
        drift = sum(q.drift_dependent for q in block)
        if drift * 2 <= len(block):
            problems.append(
                f"sessions {start}-{start + SESSIONS_PER_EPOCH - 1}: only {drift} of "
                f"{len(block)} questions are drift-dependent"
            )
    return problems


def _check_note(stream: Stream, note: InjectedNote, facts: dict[str, Fact]) -> list[str]:
    after = note.after_session
    label = f"false note after session {after}"
    problems: list[str] = []
    if count_tokens(note.text) > MAX_NOTE_TOKENS:
        problems.append(f"{label}: text exceeds {MAX_NOTE_TOKENS} tokens")
    if note.marker not in note.text:
        problems.append(f"{label}: marker is not part of the text")
    if not any(note.states_claim(line) for line in note.text.splitlines()):
        problems.append(f"{label}: no line of the text matches its claim patterns")
    fact = facts.get(note.fact_id)
    if fact is None:
        return problems + [f"{label}: unknown fact {note.fact_id}"]
    if any(note.states_claim(line) for line in _true_statements(stream, note, fact)):
        problems.append(f"{label}: its claim patterns also match the true rule")
    if not fact.leaf:
        problems.append(f"{label}: fact {fact.fact_id} is not a leaf rule")
    window = range(after + 1, after + INJECTION_WINDOW + 1)
    if not fact.valid_at(after) or (
        fact.valid_until_session is not None and fact.valid_until_session < after + INJECTION_WINDOW
    ):
        problems.append(f"{label}: fact {fact.fact_id} is not valid through session {window[-1]}")
    if note.false_value == fact.value:
        problems.append(f"{label}: false value equals the true value")
    using = [q for q in stream.questions if note.fact_id in q.fact_ids]
    targets = [q for q in using if q.session in window and q.injected_target]
    if not targets:
        problems.append(
            f"{label}: no injected_target question in sessions {window[0]}-{window[-1]}"
        )
    problems += [
        f"{label}: target {q.qid} is drift-dependent" for q in targets if q.drift_dependent
    ]
    problems += [
        f"{label}: {q.qid} uses the fact in the window but is not injected_target"
        for q in using
        if q.session in window and not q.injected_target
    ]
    evidence = [q for q in using if q.session == after]
    if len(evidence) != 1:
        problems.append(
            f"{label}: session {after} has {len(evidence)} questions on the fact, expected 1"
        )
    problems += [
        f"{label}: {q.qid} uses the fact outside sessions {after}-{window[-1]}"
        for q in using
        if not after <= q.session <= window[-1]
    ]
    return problems


def _true_statements(stream: Stream, note: InjectedNote, fact: Fact) -> list[str]:
    """Lines that state the true rule, as the note would and as memory tags them.

    Consolidation tags facts with `(verified sN)` and history with `sA-sB`, so a claim
    pattern such as a code `S5` must not match those tags on a true line either.
    """
    corrected = note.text.replace(note.false_value, fact.value).splitlines()
    plain = f"{fact.subject}: {fact.value}"
    tagged = [
        f"{plain} (verified s{session}); was {fact.value} in s1-s{session}"
        for session in range(1, stream.sessions + 1)
    ]
    return [*corrected, plain, f"{plain} (unverified)", *tagged]


def _check_injections(stream: Stream, facts: dict[str, Fact]) -> list[str]:
    notes = stream.injections
    if not notes:
        return [
            f"{q.qid}: injected_target without any false note"
            for q in stream.questions
            if q.injected_target
        ]
    problems: list[str] = []
    if sorted(n.after_session for n in notes) != list(INJECTION_AFTER_SESSIONS):
        problems.append(f"false notes must follow sessions {list(INJECTION_AFTER_SESSIONS)}")
    markers = [n.marker for n in notes]
    if len(set(markers)) != len(markers):
        problems.append("false-note markers are not unique")
    for note in notes:
        problems += _check_note(stream, note, facts)
    targeted = {n.fact_id for n in notes}
    problems += [
        f"{q.qid}: injected_target but uses no false-note fact"
        for q in stream.questions
        if q.injected_target and not targeted & set(q.fact_ids)
    ]
    return problems


def validate_stream(stream: Stream) -> list[str]:
    """Return every soundness problem of `stream`; an empty list means sound.

    Checks that each epoch's gold SQL reproduces the independently computed
    expected answer, that no expected answer is empty, that each migration has a
    DDL-visible and a semantic change, that drift-dependent questions really change
    (answer and previous-epoch SQL) while the others do not, that drift-dependent
    questions are the majority after each migration, and that false notes sit on
    leaf facts with the evidence and target questions placed around them.

    Returns
    -------
    list[str]
        One human-readable line per problem; empty when the stream is sound.
    """
    facts = {f.fact_id: f for f in stream.facts}
    problems = _check_layout(stream)
    for question in stream.questions:
        problems += _check_question(stream, question, facts)
    problems += _check_migrations(stream)
    problems += _check_drift_majority(stream)
    problems += _check_injections(stream, facts)
    return problems
