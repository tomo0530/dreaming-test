"""Data shapes of a SchemaDrift-SQL stream. Generation and grading live elsewhere."""

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


class InjectedNote(BaseModel):
    after_session: int
    fact_id: str
    false_value: str
    text: str
    # A sentence unique to this false note; its presence in memory means the note survives.
    marker: str
    # Literal texts that together state the false claim: a memory line that contains all of
    # them still carries the claim, however it was reworded. Matching is smart-case: a pattern
    # in lower case ignores case, one with an upper-case letter (a code such as `S5`) does not,
    # so that the tag `(verified s5)` on a true line never counts as the code `S5`.
    claim_patterns: list[str] = Field(min_length=1)

    def states_claim(self, line: str) -> bool:
        """Whether `line` contains every claim pattern (smart-case, see `claim_patterns`)."""
        folded = line.casefold()
        return all(
            pattern in line if pattern != pattern.casefold() else pattern in folded
            for pattern in self.claim_patterns
        )

    def present_in(self, line: str) -> bool:
        """Whether `line` carries this note: its marker verbatim, or its claim reworded."""
        return self.marker in line or self.states_claim(line)


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
