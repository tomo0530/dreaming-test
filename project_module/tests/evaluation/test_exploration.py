"""Mechanical exploration and memory-text indicators of the follow-up (20_preregistration 4)."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from project_module.evaluation.exploration import (
    QuestionFeatures,
    count_change_records,
    count_void_policy_lines,
    question_blocks,
    question_drift_kinds,
    question_features,
    reference_table,
    void_columns,
)
from project_module.schemadrift.generator import generate_stream
from project_module.schemadrift.models import Fact
from project_module.transcript import TranscriptWriter

from .builders import make_stream

REF = "lkp_values"

# One query call: (sql, result text, whether the result is an error).
Call = tuple[str, str, bool]


def _transcript(tmp_path: Path, questions: Sequence[Sequence[Call]]) -> str:
    """A transcript written by the harness's own writer, one block per question."""
    path = tmp_path / "session_001.txt"
    with TranscriptWriter(path, stream_id="s", session=1) as writer:
        for number, calls in enumerate(questions, start=1):
            writer.user(f"Question {number}?\nSecond line of the question.")
            for sql, result, error in calls:
                writer.tool_call("query", {"sql": sql})
                writer.tool_result("query", result, error=error)
            writer.tool_call("submit_sql", {"sql": "SELECT 1"})
    return path.read_text(encoding="utf-8")


def _features_of_one(tmp_path: Path, sql: str, *, result: str = "cd\tmeaning\n1\tx") -> bool:
    error = result.startswith("ERROR:")
    transcript = _transcript(tmp_path, [[(sql, result, error)]])
    return question_features(transcript, ref_table=REF, qids=["q1"])[0].unfiltered_ref_query


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select * from lkp_values", True),
        ("SELECT  code_set, cd\n  FROM   LKP_VALUES ORDER BY cd", True),
        ("select l.cd from ord_hdr o join lkp_values l using (cd)", True),
        ("select * from lkp_values where lkp_type='x'", False),
        ("select * from lkp_values limit 5", False),
        ("select count(*) from lkp_values", False),
        ("select code_set from lkp_values group by code_set", False),
        ("pragma table_info(lkp_values)", False),
        ("select * from ord_hdr o join lkp_values l on l.cd = o.st", False),
        ("select * from lkp_values_old", False),
    ],
    ids=[
        "plain",
        "case-and-spacing",
        "join",
        "where",
        "limit",
        "count",
        "group-by",
        "pragma",
        "join-on",
        "other-table",
    ],
)
def test_unfiltered_ref_query_definition(tmp_path: Path, sql: str, expected: bool) -> None:
    assert _features_of_one(tmp_path, sql) is expected


def test_unfiltered_ref_query_ignores_failed_query(tmp_path: Path) -> None:
    assert not _features_of_one(tmp_path, "select * from lkp_values", result="ERROR: locked")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("pragma table_info(x)", True),
        ("  PRAGMA table_list", True),
        ("select name from sqlite_master", True),
        ("select 'pragma'", False),
    ],
    ids=["pragma", "pragma-leading-space", "sqlite-master", "pragma-literal"],
)
def test_schema_inspection_definition(tmp_path: Path, sql: str, expected: bool) -> None:
    transcript = _transcript(tmp_path, [[(sql, "name\nx", False)]])

    features = question_features(transcript, ref_table=REF, qids=["q1"])

    assert features[0].schema_inspection is expected


def test_question_features_split_by_question_and_count_successful_queries(
    tmp_path: Path,
) -> None:
    transcript = _transcript(
        tmp_path,
        [
            [("select name from sqlite_master", "name\nx", False)],
            [
                ("select * from lkp_values", "ERROR: no such table", True),
                ("select * from ord_hdr", "oid\n1", False),
                ("select * from lkp_values", "cd\n1", False),
            ],
            [],
        ],
    )

    features = question_features(transcript, ref_table=REF, qids=["a", "b", "c"])

    assert features == [
        QuestionFeatures(qid="a", queries=1, unfiltered_ref_query=False, schema_inspection=True),
        QuestionFeatures(qid="b", queries=2, unfiltered_ref_query=True, schema_inspection=False),
        QuestionFeatures(qid="c", queries=0, unfiltered_ref_query=False, schema_inspection=False),
    ]


def test_query_result_quoting_user_label_does_not_start_a_question(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, [[("select note from t", "note\nUSER: hi", False)]])

    assert len(question_blocks(transcript)) == 1


def test_question_features_reject_qid_count_mismatch(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, [[], []])

    with pytest.raises(ValueError, match="2 questions but 3 qids"):
        question_features(transcript, ref_table=REF, qids=["a", "b", "c"])


def test_reference_table_is_the_lookup_table_of_epoch_one(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "epoch_1.sqlite") as connection:
        connection.execute("CREATE TABLE ord_hdr (oid INTEGER)")
        connection.execute("CREATE TABLE code_lookup (cd TEXT)")
    connection.close()

    assert reference_table(tmp_path) == "code_lookup"


def test_reference_table_requires_exactly_one_candidate(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "epoch_1.sqlite") as connection:
        connection.execute("CREATE TABLE ord_hdr (oid INTEGER)")
    connection.close()

    with pytest.raises(ValueError, match="expected one reference table"):
        reference_table(tmp_path)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("- status was 3 in s1-s4", 1),
        ("Shipments moved to parcel_tracking in session 5", 1),
        ("No schema changes since s1", 0),
        ("The status code was not changed in s5", 0),
        ("Codes remain unchanged; were checked in s3", 0),
        ("CONFLICT: delivered is 5 or 6", 1),
        ("  * conflict: two codes for Delivered", 1),
        ("Delivered = 6 (verified s5)", 0),
        ("Totals were in cents", 0),
    ],
    ids=[
        "history",
        "session-word",
        "negated-no",
        "negated-not",
        "negated-unchanged",
        "conflict",
        "conflict-bullet",
        "no-change-verb",
        "no-session",
    ],
)
def test_change_records_skip_negated_lines(line: str, expected: int) -> None:
    assert count_change_records(line) == expected


def test_change_records_count_lines_of_a_document() -> None:
    text = "# Memory\n- status was 3 in s1\n- Delivered = 6\nCONFLICT: x\n"

    assert count_change_records(text) == 2


def test_void_policy_lines_need_column_and_negation() -> None:
    text = "\n".join(
        [
            "- Do not filter on void_flg; it is always 0",
            "- Never count orders with VOID_FLG = 1",
            "- void_flg marks voided orders",
            "- Do not use shp_log",
            "- avoid voided rows: void_flg = 1",
        ]
    )

    assert count_void_policy_lines(text, void_columns=["void_flg"]) == 3


def test_void_policy_lines_without_void_columns_are_zero() -> None:
    assert count_void_policy_lines("never use void_flg", void_columns=[]) == 0


def test_void_columns_take_the_column_of_soft_delete_facts() -> None:
    stream = make_stream("s").model_copy(
        update={
            "facts": [
                Fact(
                    fact_id="softdelete.orders@s9",
                    kind="soft_delete",
                    subject="orders",
                    value="ord_hdr.void_flg = 1",
                    valid_from_session=9,
                    valid_until_session=None,
                    leaf=False,
                ),
                Fact(
                    fact_id="column.region@s1",
                    kind="column_name",
                    subject="region",
                    value="cust_master.zone_cd",
                    valid_from_session=1,
                    valid_until_session=None,
                    leaf=False,
                ),
            ]
        }
    )

    assert void_columns(stream) == ["void_flg"]


def test_drift_kind_matches_drift_dependent(tmp_path: Path) -> None:
    stream = generate_stream("s", 4501, tmp_path, inject=False)

    kinds = question_drift_kinds(stream)

    with_kind = {qid for qid, found in kinds.items() if found}
    dependent = {q.qid for q in stream.questions if q.drift_dependent}
    assert dependent
    assert with_kind == dependent


def test_drift_kinds_are_empty_in_epoch_one(tmp_path: Path) -> None:
    stream = generate_stream("s", 4502, tmp_path, inject=False)

    kinds = question_drift_kinds(stream)

    assert all(not kinds[q.qid] for q in stream.questions if q.session <= 4)
