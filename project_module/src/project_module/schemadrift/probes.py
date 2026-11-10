"""Memory probes: short-answer questions on the rules that hold at a checkpoint."""

import random
from collections.abc import Mapping

from project_module.schemadrift.generator import TIME_FORMAT_LABELS
from project_module.schemadrift.models import Fact, FactKind, Probe, Stream

_ANSWER_FORMS: Mapping[FactKind, str] = {
    "column_name": "Answer as table.column only.",
    "join_key": "Answer as table.column = table.column only.",
    "unit": "Answer with one word: cents or dollars.",
    "time_format": f"Answer with exactly one of: {'; '.join(TIME_FORMAT_LABELS.values())}.",
    "status_code": "Answer with the code only.",
    "soft_delete": "Answer as table.column = value only.",
    "stale_table": "Answer with the table name only.",
}
_UNIT_ALIASES: Mapping[str, list[str]] = {
    "cents": ["cent", "us cents"],
    "dollars": ["dollar", "us dollars", "usd"],
}
_TIME_ALIASES: Mapping[str, list[str]] = {
    TIME_FORMAT_LABELS["unix"]: ["unix seconds", "epoch seconds", "unix timestamp", "unix time"],
    TIME_FORMAT_LABELS["iso"]: ["iso text", "iso 8601", "iso"],
    TIME_FORMAT_LABELS["yyyymmdd"]: ["yyyymmdd", "yyyymmdd integer"],
}


def _aliases(fact: Fact) -> list[str]:
    match fact.kind:
        case "column_name" | "soft_delete":
            # The answer without its table prefix.
            return [fact.value.split(".", 1)[1]]
        case "join_key":
            left, right = fact.value.split(" = ")
            return [f"{right} = {left}"]
        case "unit":
            return list(_UNIT_ALIASES.get(fact.value, []))
        case "time_format":
            return list(_TIME_ALIASES.get(fact.value, []))
        case _:
            return []


def _probe(fact: Fact, *, injected: bool) -> Probe:
    return Probe(
        fact_id=fact.fact_id,
        question=f"In the current database, what is the {fact.subject}? {_ANSWER_FORMS[fact.kind]}",
        answer=fact.value,
        aliases=_aliases(fact),
        injected=injected,
    )


def make_probes(stream: Stream, session: int, n: int) -> list[Probe]:
    """Draw `n` probes, without replacement, from the facts valid in `session`.

    Facts targeted by a false note are always included while valid. The draw uses
    the seed `stream.seed * 100 + session`, so it is deterministic per checkpoint.

    Parameters
    ----------
    stream
        Source of the fact ledger and the false notes.
    session
        The checkpoint session.
    n
        Number of probes; fewer are returned when fewer facts are valid.

    Returns
    -------
    list[Probe]
        Sorted by `fact_id`. A probe is `injected` once the false note on its fact
        has been written, i.e. the note's `after_session` is at or before `session`.

    Raises
    ------
    ValueError
        If `n` is negative.
    """
    if n < 0:
        raise ValueError("n must not be negative")
    valid = stream.valid_facts(session)
    targets = {note.fact_id for note in stream.injections}
    written = {note.fact_id for note in stream.injections if note.after_session <= session}
    forced = [f for f in valid if f.fact_id in targets][:n]
    others = [f for f in valid if f.fact_id not in targets]
    rng = random.Random(stream.seed * 100 + session)
    drawn = rng.sample(others, min(n - len(forced), len(others)))
    chosen = sorted(forced + drawn, key=lambda f: f.fact_id)
    return [_probe(f, injected=f.fact_id in written) for f in chosen]


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def probe_correct(probe: Probe, answer: str) -> bool:
    """Grade a short answer against the probe's answer and aliases.

    Parameters
    ----------
    probe
        The probe that was asked.
    answer
        The model's answer.

    Returns
    -------
    bool
        True when `answer` equals the answer or an alias after casefolding and
        dropping every character that is not a letter or digit.
    """
    given = _normalize(answer)
    return bool(given) and given in {_normalize(a) for a in [probe.answer, *probe.aliases]}
