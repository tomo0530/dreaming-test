"""Mechanical indicators read from transcripts, memory text and streams (20_preregistration 4).

Everything here is counted by code; no judge model is involved. SQL is matched after lower-casing
and collapsing runs of whitespace, and only successful `query` calls count (a result that is not
labelled as an error and does not start with `ERROR:`).
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel

from project_module.agent.session import QUERY_TOOL
from project_module.llm.loop import ERROR_PREFIX
from project_module.schemadrift.models import SESSIONS_PER_EPOCH, Stream

# Names the generator gives the code lookup table; exactly one exists in each stream.
REFERENCE_TABLE_CANDIDATES = ("ref_codes", "code_lookup", "lkp_values")
_USER_LABEL = "USER:"
_CALL_PREFIX = f"TOOL CALL {QUERY_TOOL}: "
_RESULT_LABEL = re.compile(rf"^TOOL RESULT \({QUERY_TOOL}\)(?P<error> \[error\])?: ?(?P<text>.*)$")
# An unfiltered reference-table query has none of these (substring match on normalised SQL).
_FILTER_MARKERS = ("where", " on ", "limit")
_SCHEMA_MARKERS = ("sqlite_master", "pragma")
_AGGREGATE_MARKERS = ("count(", "sum(", "min(", "max(", "avg(", "group by")
_WHITESPACE = re.compile(r"\s+")

_CHANGE_HISTORY = re.compile(
    r"\b(was|were|changed|renamed|moved|replaced|switched)\b.*\bs(ession )?\d+\b", re.IGNORECASE
)
_CONFLICT = re.compile(r"^\s*[-*]?\s*CONFLICT:", re.IGNORECASE)
_CHANGE_NEGATION = re.compile(r"\b(no|not|never|without|unchanged)\b", re.IGNORECASE)
_POLICY_NEGATION = re.compile(
    r"\b(do not|don't|never|avoid|should not|must not|no need)\b", re.IGNORECASE
)
_QUALIFIED_COLUMN = re.compile(r"\b[A-Za-z_]\w*\.([A-Za-z_]\w*)\b")


class QuestionFeatures(BaseModel):
    """Exploration behaviour of the agent on one question.

    `queries` counts the successful `query` calls; the two flags say whether at least one of them
    was an unfiltered reference-table read or a schema inspection.
    """

    qid: str
    queries: int
    unfiltered_ref_query: bool
    schema_inspection: bool


def normalize_sql(sql: str) -> str:
    """Lower-case `sql`, collapse whitespace runs to one space and strip the ends."""
    return _WHITESPACE.sub(" ", sql.lower()).strip()


def is_unfiltered_ref_query(sql: str, *, ref_table: str) -> bool:
    """Whether `sql` reads `ref_table` without filters, limits, schema access or aggregation."""
    text = normalize_sql(sql)
    table = re.escape(ref_table.lower())
    if re.search(rf"\b(from|join) {table}\b", text) is None:
        return False
    markers = (*_FILTER_MARKERS, *_SCHEMA_MARKERS, *_AGGREGATE_MARKERS)
    return not any(marker in text for marker in markers)


def is_schema_inspection(sql: str) -> bool:
    """Whether `sql` starts with `pragma` or reads `sqlite_master`."""
    text = normalize_sql(sql)
    return text.startswith("pragma") or "from sqlite_master" in text


def _records(lines: Sequence[str]) -> list[str]:
    """Join each labelled line with its continuation lines (which start with one space)."""
    records: list[str] = []
    for line in lines:
        if line.startswith(" ") and records:
            records[-1] += "\n" + line[1:]
        else:
            records.append(line)
    return records


def question_blocks(transcript: str) -> list[list[str]]:
    """The records of each question, split at every `USER:` record; the header is dropped."""
    blocks: list[list[str]] = []
    for record in _records(transcript.splitlines()):
        if record.startswith(_USER_LABEL):
            blocks.append([record])
        elif blocks:
            blocks[-1].append(record)
    return blocks


def _call_sql(record: str) -> str | None:
    try:
        arguments = json.loads(record.removeprefix(_CALL_PREFIX))
    except json.JSONDecodeError:
        return None
    sql = arguments.get("sql") if isinstance(arguments, dict) else None
    return sql if isinstance(sql, str) else None


def _successful_queries(block: Sequence[str]) -> list[str]:
    """SQL of each `query` call whose very next record is its non-error result."""
    queries: list[str] = []
    for call, following in zip(block, [*block[1:], ""], strict=True):
        if not call.startswith(_CALL_PREFIX):
            continue
        sql = _call_sql(call)
        match = _RESULT_LABEL.match(following.split("\n", 1)[0])
        if sql is None or match is None or match["error"]:
            continue
        if not match["text"].startswith(ERROR_PREFIX):
            queries.append(sql)
    return queries


def question_features(
    transcript: str, *, ref_table: str, qids: Sequence[str]
) -> list[QuestionFeatures]:
    """Exploration features of each question of one session transcript.

    Parameters
    ----------
    transcript
        Text of one session transcript.
    ref_table
        The stream's reference table (see `reference_table`).
    qids
        Ids of the session's questions in the order they were asked; the transcript does not
        record them.

    Raises
    ------
    ValueError
        If the transcript does not hold exactly one question per qid.
    """
    blocks = question_blocks(transcript)
    if len(blocks) != len(qids):
        raise ValueError(f"transcript has {len(blocks)} questions but {len(qids)} qids")
    features: list[QuestionFeatures] = []
    for qid, block in zip(qids, blocks, strict=True):
        queries = _successful_queries(block)
        features.append(
            QuestionFeatures(
                qid=qid,
                queries=len(queries),
                unfiltered_ref_query=any(
                    is_unfiltered_ref_query(sql, ref_table=ref_table) for sql in queries
                ),
                schema_inspection=any(is_schema_inspection(sql) for sql in queries),
            )
        )
    return features


def reference_table(stream_root: Path) -> str:
    """The reference table that exists in the stream's epoch-1 database.

    Raises
    ------
    FileNotFoundError
        If the epoch-1 database does not exist.
    ValueError
        If not exactly one of `REFERENCE_TABLE_CANDIDATES` exists in it.
    """
    database = stream_root / "epoch_1.sqlite"
    if not database.is_file():
        raise FileNotFoundError(f"epoch-1 database not found: {database}")
    uri = f"{database.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    found = sorted({str(name) for (name,) in rows} & set(REFERENCE_TABLE_CANDIDATES))
    if len(found) != 1:
        raise ValueError(f"{database}: expected one reference table, found {found}")
    return found[0]


def count_change_records(text: str) -> int:
    """Lines that record a change or a conflict and contain no negation word."""
    return sum(
        1
        for line in text.splitlines()
        if (_CHANGE_HISTORY.search(line) or _CONFLICT.search(line))
        and not _CHANGE_NEGATION.search(line)
    )


def count_void_policy_lines(text: str, *, void_columns: Sequence[str]) -> int:
    """Lines that name a void column and contain a prohibition or negation phrase."""
    columns = [column.lower() for column in void_columns]
    if not columns:
        return 0
    return sum(
        1
        for line in text.splitlines()
        if any(column in line.lower() for column in columns) and _POLICY_NEGATION.search(line)
    )


def void_columns(stream: Stream) -> list[str]:
    """Column part of the first `<table>.<column>` in each soft-delete fact's value."""
    columns: list[str] = []
    for fact in stream.facts:
        if fact.kind != "soft_delete":
            continue
        match = _QUALIFIED_COLUMN.search(fact.value)
        if match is not None and match[1] not in columns:
            columns.append(match[1])
    return columns


def _epoch_start(epoch: int) -> int:
    return (epoch - 1) * SESSIONS_PER_EPOCH + 1


def question_drift_kinds(stream: Stream) -> dict[str, list[str]]:
    """Drift kinds of each question, by qid (20_preregistration 3).

    The kinds of the question's facts whose value changed at the start of the question's epoch;
    questions of epoch 1, and questions with no such fact, have none.
    """
    kinds: dict[str, list[str]] = {}
    for question in stream.questions:
        epoch = stream.epoch_of(question.session)
        changed = [
            fact.kind
            for fact in map(stream.fact, question.fact_ids)
            if epoch >= 2 and fact.valid_from_session == _epoch_start(epoch)
        ]
        kinds[question.qid] = sorted(set(changed))
    return kinds
