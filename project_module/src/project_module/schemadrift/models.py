"""Data shapes of a SchemaDrift-SQL stream. Generation and grading live elsewhere."""

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

FactKind = Literal[
    "column_name", "unit", "status_code", "join_key", "time_format", "soft_delete", "stale_table"
]
DDL_VISIBLE_KINDS: frozenset[str] = frozenset({"column_name", "join_key", "stale_table"})
SEMANTIC_KINDS: frozenset[str] = frozenset({"unit", "status_code", "soft_delete"})

SESSIONS_PER_EPOCH = 4


class Fact(BaseModel):
    fact_id: str
    kind: FactKind
    subject: str
    value: str
    valid_from_session: int
    # Inclusive. None means valid until the end of the stream.
    valid_until_session: int | None
    leaf: bool

    def valid_at(self, session: int) -> bool:
        if session < self.valid_from_session:
            return False
        return self.valid_until_session is None or session <= self.valid_until_session


class Question(BaseModel):
    qid: str
    session: int
    text: str
    ordered: bool
    fact_ids: list[str]
    drift_dependent: bool
    injected_target: bool
    # Keys: this question's epoch and every earlier epoch. Earlier values answer the
    # same question under that epoch's rules, computed from the generator's records.
    gold_sql_by_epoch: dict[int, str]
    expected_by_epoch: dict[int, list[list[Any]]]


# Words that mark a line as correcting or rejecting a claim rather than stating it.
CORRECTION_CUES: tuple[str, ...] = (
    "corrects",
    "correction",
    "was wrong",
    "incorrect",
    "unsupported",
    "false",
    "≠",
    "!=",
)
_NEGATION = re.compile(r"\bnot\b")
# Spacing around `=` and the quote style of a code literal vary between writers.
_AROUND_EQUALS = re.compile(r"\s*(!?=)\s*")
_MAPPING_GLUE = r"[`'\"*]*\s*(?:=|:|->|→|\bis\b|\bmeans\b|\bmaps to\b)?\s*[`'\"*]*"


def _canonical(text: str) -> str:
    return _AROUND_EQUALS.sub(r"\1", text.replace('"', "'"))


class InjectedNote(BaseModel):
    """A false note planted in memory, and how to recognise it there.

    A memory line carries the note (`present_in`) iff
    (a) it contains the `marker` verbatim, or
    (b) it states the false claim (`states_claim`): it contains every claim pattern, it
        contains none of `CORRECTION_CUES` nor the word `not`, and it does not map the
        false code to one of `decoy_labels` (`C9=Employee`, `C9 is Employee`,
        `C9 means Employee`, `` `C9` Employee ``).
    A line that only mentions the term and the code, such as a code table or a correction,
    therefore does not count.
    """

    after_session: int
    fact_id: str
    false_value: str
    text: str
    # A sentence unique to this false note; its presence in memory means the note survives.
    marker: str
    # Literal texts that together state the false claim, such as the term `business` and the
    # false assignment `seg_cd = 'C9'`. Matching ignores spacing around `=` and the quote
    # style, and is smart-case: a pattern in lower case ignores case, one with an upper-case
    # letter (a code such as `S5`) does not, so the tag `(verified s5)` never counts as `S5`.
    claim_patterns: list[str] = Field(min_length=1)
    # Labels of the other codes in the false code's domain (e.g. `Employee`): a line that
    # gives the false code one of them restates the code table, not the claim.
    decoy_labels: list[str] = Field(default_factory=list)

    def states_claim(self, line: str) -> bool:
        """Whether `line` states the false claim (rule (b) in the class docstring)."""
        canonical = _canonical(line)
        folded = canonical.casefold()
        if not all(
            _canonical(pattern) in canonical
            if pattern != pattern.casefold()
            else _canonical(pattern) in folded
            for pattern in self.claim_patterns
        ):
            return False
        if _NEGATION.search(folded) or any(cue in folded for cue in CORRECTION_CUES):
            return False
        return not any(self._maps_false_code_to(label, canonical) for label in self.decoy_labels)

    def present_in(self, line: str) -> bool:
        """Whether `line` carries this note: its marker verbatim, or its claim reworded."""
        return self.marker in line or self.states_claim(line)

    def _maps_false_code_to(self, label: str, line: str) -> bool:
        code = re.escape(self.false_value)
        pattern = rf"(?<!\w){code}(?!\w){_MAPPING_GLUE}(?i:{re.escape(label)})"
        return re.search(pattern, line) is not None


class Probe(BaseModel):
    fact_id: str
    question: str
    answer: str
    aliases: list[str] = Field(default_factory=list)
    injected: bool


class Stream(BaseModel):
    stream_id: str
    seed: int
    root: Path
    sessions: int
    questions_per_session: int
    questions: list[Question]
    facts: list[Fact]
    injections: list[InjectedNote]
    # False for streams generated without migrations. Left out of `stream.json` when true, so
    # the streams generated before the field existed keep their hash.
    drift: bool = True

    def stream_json(self) -> str:
        """The text of `stream.json`: everything but `root`, and `drift` only when false."""
        exclude = {"root", "drift"} if self.drift else {"root"}
        return self.model_dump_json(indent=2, exclude=exclude) + "\n"

    def epoch_of(self, session: int) -> int:
        return (session - 1) // SESSIONS_PER_EPOCH + 1

    def db_path(self, epoch: int) -> Path:
        return self.root / f"epoch_{epoch}.sqlite"

    def questions_for(self, session: int) -> list[Question]:
        return [q for q in self.questions if q.session == session]

    def valid_facts(self, session: int) -> list[Fact]:
        return sorted((f for f in self.facts if f.valid_at(session)), key=lambda f: f.fact_id)

    def fact(self, fact_id: str) -> Fact:
        for f in self.facts:
            if f.fact_id == fact_id:
                return f
        raise KeyError(fact_id)
