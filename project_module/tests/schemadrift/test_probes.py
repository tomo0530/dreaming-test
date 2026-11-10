from pathlib import Path

import pytest
from project_module.schemadrift.generator import generate_stream
from project_module.schemadrift.models import Probe
from project_module.schemadrift.probes import make_probes, probe_correct


def test_make_probes_deterministic_and_includes_injected(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=True)
    first_target = stream.injections[0].fact_id

    probes = make_probes(stream, 4, 8)

    assert probes == make_probes(stream, 4, 8)
    assert len(probes) == 8
    assert len({p.fact_id for p in probes}) == 8
    assert [p.injected for p in probes if p.fact_id == first_target] == [True]


def test_make_probes_draws_only_facts_valid_in_the_session(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=True)
    valid_in_8 = {f.fact_id for f in stream.valid_facts(8)}

    probes = make_probes(stream, 8, 8)

    assert {p.fact_id for p in probes} <= valid_in_8


def test_make_probes_marks_a_target_not_yet_injected_as_not_injected(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=True)
    later_target = stream.injections[1].fact_id

    probes = make_probes(stream, 4, 8)

    assert [p.injected for p in probes if p.fact_id == later_target] == [False]


def test_make_probes_without_injections_flags_nothing(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=False)

    probes = make_probes(stream, 12, 8)

    assert len(probes) == 8
    assert not any(p.injected for p in probes)


def test_make_probes_rejects_a_negative_count(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=False)

    with pytest.raises(ValueError, match="negative"):
        make_probes(stream, 4, -1)


def test_every_fact_probe_accepts_its_own_answer(tmp_path: Path) -> None:
    stream = generate_stream("s", 23, tmp_path, inject=True)

    probes = make_probes(stream, 12, 100)

    assert len(probes) == len(stream.valid_facts(12))
    assert all(probe_correct(p, p.answer) for p in probes)
    assert all(p.question.startswith("In the current database, what is the ") for p in probes)


def _probe(answer: str, aliases: list[str] | None = None) -> Probe:
    return Probe(fact_id="f", question="q", answer=answer, aliases=aliases or [], injected=False)


@pytest.mark.parametrize(
    ("probe", "answer", "correct"),
    [
        (_probe("orders.buyer_ref = customers.cid"), " Orders.Buyer_Ref=customers.CID ", True),
        (
            _probe("orders.buyer_ref = customers.cid", ["customers.cid = orders.buyer_ref"]),
            "customers.cid = orders.buyer_ref",
            True,
        ),
        (_probe("cents", ["cent", "us cents"]), "Cents.", True),
        (_probe("cents", ["cent", "us cents"]), "US cents", True),
        (_probe("K4"), "`k4`", True),
        (_probe("K4"), "K5", False),
        (_probe("cents", ["cent"]), "dollars", False),
        (_probe("K4"), "", False),
        (_probe("K4"), "The code is K4", False),
    ],
    ids=[
        "case-space-punctuation",
        "alias",
        "trailing-period",
        "multi-word-alias",
        "backticks",
        "wrong-code",
        "wrong-unit",
        "empty",
        "extra-words",
    ],
)
def test_probe_correct_normalizes(probe: Probe, answer: str, correct: bool) -> None:
    assert probe_correct(probe, answer) is correct
