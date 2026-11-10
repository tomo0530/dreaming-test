"""Tests for the CL-Bench adapter. Everything runs on tiny synthetic data created in tmp."""

import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from project_module.clbench import (
    CLBENCH_DB_SIZES,
    CLBENCH_SHA,
    DRIFT_NOTICE,
    MAX_QUERIES,
    MAX_RESULT_ROWS,
    SQL_TIMEOUT_S,
    CLBenchEnv,
    FileRecord,
    evaluate_answer,
    fetch_clbench,
    reward,
)
from project_module.config import Budgets, ExperimentConfig, OnlineBudget, load_experiment
from project_module.contracts import Grade, TaskQuestion
from project_module.runner import make_env

from project_module import clbench

PRE_IDS = list(range(1, 31))  # the official file holds 30; only the first 20 are used
POST_IDS = list(range(101, 121))

# Official ordering (seed 42, run offset 100_003 per run) for these synthetic ids.
OFFICIAL_PRE_ORDER = {
    0: [1, 5, 3, 12, 4, 14, 19, 15, 16, 6, 17, 10, 13, 20, 7, 11, 18, 9, 2, 8],
    1: [17, 10, 19, 13, 1, 3, 7, 16, 15, 4, 18, 9, 11, 8, 20, 2, 14, 12, 6, 5],
}
OFFICIAL_POST_ORDER = {
    0: [
        114,
        119,
        103,
        112,
        101,
        113,
        105,
        110,
        108,
        106,
        115,
        118,
        109,
        107,
        120,
        117,
        104,
        111,
        102,
        116,
    ],
    1: [
        105,
        112,
        116,
        119,
        109,
        107,
        101,
        108,
        120,
        117,
        103,
        111,
        115,
        114,
        118,
        106,
        113,
        110,
        102,
        104,
    ],
}


def _question_json(question_id: int) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "question": f"Synthetic question {question_id}?",
        "sql": "SELECT 1",
        "answer": question_id,
        "answer_type": "integer",
        "difficulty": "easy",
        "tolerance": 0.0,
    }


def _make_db(path: Path, *, marker: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)")
        conn.executemany(
            "INSERT INTO items VALUES (?, ?)", [(i, f"{marker}-{i}") for i in range(60)]
        )
        conn.execute("CREATE TABLE tiny (v TEXT)")
        conn.execute("INSERT INTO tiny VALUES (?)", (marker,))
        conn.commit()


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "clbench"
    root.mkdir(parents=True)
    (root / "questions.json").write_text(json.dumps([_question_json(i) for i in PRE_IDS]))
    (root / "questions_post_drift.json").write_text(
        json.dumps([_question_json(i) for i in POST_IDS])
    )
    _make_db(root / "products.db", marker="pre")
    _make_db(root / "products_drifted.db", marker="post")
    return root


def _make_env(tmp_path: Path, *, run_index: int = 0, questions_per_session: int = 5) -> CLBenchEnv:
    return CLBenchEnv(
        _make_root(tmp_path), run_index=run_index, questions_per_session=questions_per_session
    )


def _all_questions(env: CLBenchEnv) -> list[TaskQuestion]:
    return [q for session in range(1, env.sessions + 1) for q in env.questions_for(session)]


def _qid_numbers(questions: list[TaskQuestion]) -> list[int]:
    return [int(q.qid.removeprefix("q")) for q in questions]


def _find(env: CLBenchEnv, question_id: int) -> TaskQuestion:
    return next(q for q in _all_questions(env) if q.qid == f"q{question_id}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- scoring ----------------------------------------------------------------------------------


def test_evaluate_answer_matches_official_rules():
    assert evaluate_answer("1,234", 1, "integer", 0) is True
    assert evaluate_answer("about 10.05", 10.0, "float", 0.0) is True
    assert evaluate_answer(" Foo ", "foo", "text", 0) is True
    assert evaluate_answer("", "foo", "text", 0) is False


@pytest.mark.parametrize(
    ("answer", "expected", "answer_type", "tolerance", "outcome"),
    [
        ("There are -5 units", -5, "integer", 0.0, True),
        ("12.9", 12, "integer", 0.0, True),
        ("no digits here", 3, "integer", 0.0, False),
        ("42", "not-a-number", "integer", 0.0, False),
        ("100.9", 100.0, "float", 0.0, True),
        ("101.1", 100.0, "float", 0.0, False),
        ("10.4", 10.0, "float", 0.5, True),
        ("10.6", 10.0, "float", 0.5, False),
        ("no digits here", 1.5, "float", 0.0, False),
        ("x", "not-a-number", "float", 0.0, False),
        ("Acme  Corp", "acme corp", "text", 0.0, False),
        ("ACME CORP", "Acme Corp", "text", 0.0, True),
        ("   ", "foo", "text", 0.0, False),
    ],
    ids=[
        "integer-first-signed-int",
        "integer-ignores-fraction",
        "integer-no-digits",
        "integer-bad-expected",
        "float-one-percent-floor",
        "float-over-one-percent",
        "float-explicit-tolerance",
        "float-over-tolerance",
        "float-no-digits",
        "float-bad-expected",
        "text-internal-whitespace-matters",
        "text-case-insensitive",
        "text-blank-answer",
    ],
)
def test_evaluate_answer_edge_cases(answer, expected, answer_type, tolerance, outcome):
    assert evaluate_answer(answer, expected, answer_type, tolerance) is outcome


def test_reward_formula():
    assert reward(True, 3) == 0.8
    assert reward(False, 3) == 0.0


def test_reward_is_zero_for_a_correct_answer_that_used_the_whole_budget():
    assert reward(True, 15) == 0.0


# --- env construction and ordering --------------------------------------------------------------


def test_env_has_forty_questions_in_eight_sessions_of_five(tmp_path):
    sut = _make_env(tmp_path)

    questions = _all_questions(sut)

    assert (sut.env_id, sut.sessions, sut.max_queries, sut.submit_param) == (
        "clbench-r0",
        8,
        15,
        "value",
    )
    assert len(questions) == 40
    assert len({q.qid for q in questions}) == 40
    assert all(len(sut.questions_for(s)) == 5 for s in range(1, 9))
    assert [q.session for q in sut.questions_for(3)] == [3] * 5


def test_env_marks_only_post_drift_questions_as_drift_dependent(tmp_path):
    sut = _make_env(tmp_path)

    flags = [[q.drift_dependent for q in sut.questions_for(s)] for s in range(1, 9)]

    assert flags == [[False] * 5] * 4 + [[True] * 5] * 4
    assert not any(q.injected_target for q in _all_questions(sut))


@pytest.mark.parametrize("run_index", [0, 1])
def test_env_permutation_follows_the_official_formula(tmp_path, run_index):
    sut = _make_env(tmp_path, run_index=run_index)

    order = _qid_numbers(_all_questions(sut))

    assert order[:20] == OFFICIAL_PRE_ORDER[run_index]
    assert order[20:] == OFFICIAL_POST_ORDER[run_index]


def test_env_permutation_is_deterministic_per_run_index(tmp_path):
    first = _qid_numbers(_all_questions(_make_env(tmp_path / "a", run_index=2)))
    second = _qid_numbers(_all_questions(_make_env(tmp_path / "b", run_index=2)))
    other = _qid_numbers(_all_questions(_make_env(tmp_path / "c", run_index=1)))

    assert first == second
    assert first != other
    assert sorted(first[:20]) == sorted(other[:20])
    assert set(first[:20]) <= set(PRE_IDS) and set(first[20:]) <= set(POST_IDS)


def test_env_prepends_notice_to_21st_question(tmp_path):
    sut = _make_env(tmp_path)

    twentieth = sut.questions_for(4)[-1]
    twenty_first = sut.questions_for(5)[0]

    assert twenty_first.text.startswith(f"{DRIFT_NOTICE}\n\nQuestion 21/40")
    assert DRIFT_NOTICE.startswith("NOTICE: The live database schema or contents may have changed")
    assert twentieth.text.startswith("Question 20/40")
    others = [q for q in _all_questions(sut) if q.qid != twenty_first.qid]
    assert not any("NOTICE" in q.text for q in others)


def test_question_text_has_question_budget_and_row_cap(tmp_path):
    sut = _make_env(tmp_path)
    first = sut.questions_for(1)[0]

    assert f"Synthetic question {first.qid.removeprefix('q')}?" in first.text
    assert "up to 15 exploratory SQL queries" in first.text
    assert "capped at 50 rows" in first.text
    assert "office products, electronics, and musical instruments" in first.text


def test_submit_tool_takes_a_single_string_value(tmp_path):
    sut = _make_env(tmp_path)

    function = sut.submit_tool()["function"]

    assert function["name"] == "submit_answer"
    assert function["parameters"]["required"] == ["value"]
    assert function["parameters"]["properties"]["value"]["type"] == "string"


@pytest.mark.parametrize("questions_per_session", [0, 3, 40])
def test_env_rejects_session_sizes_that_do_not_align_with_the_drift_boundary(
    tmp_path, questions_per_session
):
    root = _make_root(tmp_path)

    with pytest.raises(ValueError, match="questions_per_session"):
        CLBenchEnv(root, run_index=0, questions_per_session=questions_per_session)


def test_env_drift_session_moves_with_session_size(tmp_path):
    sut = _make_env(tmp_path, questions_per_session=10)

    assert sut.sessions == 4
    assert sut.run_query(2, "SELECT v FROM tiny").endswith("pre")
    assert sut.run_query(3, "SELECT v FROM tiny").endswith("post")


def test_env_rejects_negative_run_index(tmp_path):
    with pytest.raises(ValueError, match="run_index"):
        CLBenchEnv(_make_root(tmp_path), run_index=-1)


def test_env_reports_missing_data_with_fetch_hint(tmp_path):
    root = _make_root(tmp_path)
    (root / "products_drifted.db").unlink()

    with pytest.raises(FileNotFoundError, match="fetch_clbench"):
        CLBenchEnv(root, run_index=0)


def test_env_rejects_duplicate_question_ids(tmp_path):
    root = _make_root(tmp_path)
    duplicated = [_question_json(1)] * 30
    (root / "questions.json").write_text(json.dumps(duplicated))

    with pytest.raises(ValueError, match="duplicate"):
        CLBenchEnv(root, run_index=0)


def test_env_rejects_question_files_that_are_too_short(tmp_path):
    root = _make_root(tmp_path)
    (root / "questions_post_drift.json").write_text(json.dumps([_question_json(101)]))

    with pytest.raises(ValueError, match="at least 20"):
        CLBenchEnv(root, run_index=0)


def test_questions_for_rejects_sessions_out_of_range(tmp_path):
    sut = _make_env(tmp_path)

    with pytest.raises(ValueError, match="session"):
        sut.questions_for(9)
    with pytest.raises(ValueError, match="session"):
        sut.run_query(0, "SELECT 1")


# --- run_query -----------------------------------------------------------------------------------


def test_env_switches_to_drifted_db_from_session_5(tmp_path):
    sut = _make_env(tmp_path)

    results = {s: sut.run_query(s, "SELECT v FROM tiny") for s in (4, 5)}

    assert results[4].splitlines() == ["v  ", "---", "pre"]
    assert results[5].splitlines() == ["v   ", "----", "post"]


def test_env_rejects_non_select_and_supports_meta_commands(tmp_path):
    sut = _make_env(tmp_path)
    db_before = _sha256(tmp_path / "clbench" / "products.db")

    insert = sut.run_query(1, "INSERT INTO tiny VALUES ('x')")
    drop = sut.run_query(1, "drop table tiny")
    attach = sut.run_query(1, "ATTACH DATABASE 'x.db' AS x")
    tables = sut.run_query(1, ".tables")
    schema_one = sut.run_query(1, ".schema tiny")
    schema_all = sut.run_query(1, ".schema")
    pragma = sut.run_query(1, "PRAGMA table_info(tiny)")
    explain = sut.run_query(1, "EXPLAIN SELECT 1")
    with_clause = sut.run_query(1, "WITH x AS (SELECT 1 AS one) SELECT one FROM x;")

    assert (
        insert == "ERROR: Only SELECT / WITH / EXPLAIN / PRAGMA statements are allowed. Got: INSERT"
    )
    assert drop.endswith("Got: DROP")
    assert attach.endswith("Got: ATTACH")
    assert tables == "items\ntiny"
    assert schema_one == "CREATE TABLE tiny (v TEXT)"
    assert schema_all.count("CREATE TABLE") == 2
    assert pragma.splitlines()[0].startswith("cid")
    assert pragma.splitlines()[2].split("|")[1].strip() == "v"
    assert explain.startswith("addr")
    assert with_clause.splitlines()[-1].strip() == "1"
    assert _sha256(tmp_path / "clbench" / "products.db") == db_before


def test_env_cannot_write_through_a_with_clause(tmp_path):
    sut = _make_env(tmp_path)
    db = tmp_path / "clbench" / "products.db"
    db_before = _sha256(db)

    result = sut.run_query(1, "WITH x AS (SELECT 1) DELETE FROM tiny")

    assert result.startswith("ERROR:")
    assert _sha256(db) == db_before
    assert sut.run_query(1, "SELECT COUNT(*) AS n FROM tiny").splitlines()[-1].strip() == "1"


def test_env_returns_sql_errors_as_text(tmp_path):
    sut = _make_env(tmp_path)

    missing_table = sut.run_query(1, "SELECT * FROM nope")
    two_statements = sut.run_query(1, "SELECT 1; SELECT 2")
    empty = sut.run_query(1, "   ")

    assert missing_table == "ERROR: no such table: nope"
    assert two_statements.startswith("ERROR:")
    assert empty.startswith("ERROR: Only SELECT")


def test_env_caps_results_at_50_rows_and_says_so(tmp_path):
    sut = _make_env(tmp_path)

    capped = sut.run_query(1, "SELECT id FROM items ORDER BY id")
    exact = sut.run_query(1, "SELECT id FROM items WHERE id < 50")
    empty = sut.run_query(1, "SELECT id FROM items WHERE id < 0")

    capped_lines = capped.splitlines()
    assert capped_lines[-1] == "... (showing first 50 rows)"
    assert len(capped_lines) == 2 + 50 + 1
    assert "... (showing first" not in exact
    assert len(exact.splitlines()) == 2 + 50
    assert empty == "(no results)"


def test_env_formats_rows_as_padded_table_with_null(tmp_path):
    sut = _make_env(tmp_path)

    result = sut.run_query(
        1, "SELECT id, NULL AS missing, 'a b' || char(10) || 'c' AS note FROM items LIMIT 2"
    )

    assert result.splitlines() == [
        "id | missing | note ",
        "---+---------+------",
        "0  | NULL    | a b c",
        "1  | NULL    | a b c",
    ]


def test_env_times_out_runaway_queries(tmp_path, monkeypatch):
    sut = _make_env(tmp_path)
    monkeypatch.setattr(clbench, "SQL_TIMEOUT_S", 0.2)
    runaway = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT COUNT(*) FROM c"
    )

    result = sut.run_query(1, runaway)

    assert result == "ERROR: query exceeded 0.2s timeout"


# --- grading and feedback ------------------------------------------------------------------------


def test_grade_categories_and_score(tmp_path):
    sut = _make_env(tmp_path)
    question = _find(sut, 7)

    correct = sut.grade(question, "7", 3)
    wrong = sut.grade(question, "8", 3)
    blank = sut.grade(question, "", 3)
    none = sut.grade(question, None, 3)

    assert correct == Grade(category="correct", score=0.8)
    assert wrong == Grade(category="wrong_result", score=0.0)
    assert blank == Grade(category="wrong_result", score=0.0)
    assert none == Grade(category="no_submission", score=0.0)


def test_query_limit_exceeded_is_no_submission(tmp_path):
    sut = _make_env(tmp_path)
    question = _find(sut, 7)

    at_limit = sut.grade(question, "7", 15)
    over_limit = sut.grade(question, "7", 16)

    assert at_limit == Grade(category="correct", score=0.0)
    assert over_limit == Grade(category="no_submission", score=0.0)


def test_grade_uses_the_answer_type_of_each_question(tmp_path):
    root = _make_root(tmp_path)
    questions = [_question_json(i) for i in PRE_IDS]
    questions[0].update(answer=10.0, answer_type="float", tolerance=0.0)
    questions[1].update(answer="Acme Corp", answer_type="text")
    (root / "questions.json").write_text(json.dumps(questions))
    sut = CLBenchEnv(root, run_index=0)

    assert sut.grade(_find(sut, 1), "about 10.05", 1).correct
    assert not sut.grade(_find(sut, 1), "10.2", 1).correct
    assert sut.grade(_find(sut, 2), " acme corp ", 1).correct


def test_grade_rejects_unknown_questions(tmp_path):
    sut = _make_env(tmp_path)
    stranger = TaskQuestion(
        qid="q999", session=1, text="", drift_dependent=False, injected_target=False
    )

    with pytest.raises(ValueError, match="q999"):
        sut.grade(stranger, "1", 1)


def test_feedback_reveals_expected_value_only_when_wrong(tmp_path):
    sut = _make_env(tmp_path)
    question = _find(sut, 7)
    number = _qid_numbers(_all_questions(sut)).index(7) + 1

    on_correct = sut.feedback(question, sut.grade(question, "7", 2))
    on_wrong = sut.feedback(question, sut.grade(question, "8", 2))
    on_none = sut.feedback(question, sut.grade(question, None, 2))

    assert on_correct == f"Question {number}: CORRECT!"
    assert on_wrong == f"Question {number}: INCORRECT.\nCorrect answer: 7"
    assert on_none is not None and on_none.endswith("The correct answer was: 7")
    assert "SELECT" not in on_wrong + (on_none or "")


# --- fetch -------------------------------------------------------------------------------------


@contextmanager
def _serve(respond: Callable[[BaseHTTPRequestHandler], None]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            respond(self)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _send(handler: BaseHTTPRequestHandler, payload: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


@pytest.fixture
def serve(monkeypatch):
    """Local HTTP server factory; bypasses any proxy configured in the environment."""
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    return _serve


def test_download_streams_to_disk_and_returns_size_and_hash(tmp_path, serve):
    payload = b"0123456789" * 1000
    target = tmp_path / "out" / "file.bin"

    with serve(lambda handler: _send(handler, payload)) as base:
        record = clbench._download(f"{base}/f", target, token=None, expected_size=len(payload))

    assert target.read_bytes() == payload
    assert record == FileRecord(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    assert not list(target.parent.glob("*.part"))


def test_download_rejects_size_mismatch_and_leaves_no_file(tmp_path, serve):
    target = tmp_path / "file.bin"

    with serve(lambda handler: _send(handler, b"short")) as base:
        with pytest.raises(ValueError, match="size"):
            clbench._download(f"{base}/f", target, token=None, expected_size=999)

    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_download_does_not_leak_the_token_to_another_host(tmp_path, serve):
    seen_by_origin: list[str | None] = []
    seen_by_cdn: list[str | None] = []

    def cdn(handler: BaseHTTPRequestHandler) -> None:
        seen_by_cdn.append(handler.headers.get("Authorization"))
        _send(handler, b"payload")

    with serve(cdn) as cdn_base:

        def origin(handler: BaseHTTPRequestHandler) -> None:
            seen_by_origin.append(handler.headers.get("Authorization"))
            handler.send_response(302)
            handler.send_header("Location", f"{cdn_base}/signed")
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        with serve(origin) as origin_base:
            clbench._download(
                f"{origin_base}/f", tmp_path / "f", token="hf_secret", expected_size=7
            )

    assert seen_by_origin == ["Bearer hf_secret"]
    assert seen_by_cdn == [None]


def test_download_propagates_http_errors_and_cleans_up(tmp_path, serve):
    def not_found(handler: BaseHTTPRequestHandler) -> None:
        handler.send_error(404)

    with serve(not_found) as base:
        with pytest.raises(urllib.error.HTTPError):
            clbench._download(f"{base}/missing", tmp_path / "f", token=None, expected_size=None)

    assert list(tmp_path.iterdir()) == []


class _FakeDownloads:
    """Stands in for the network: writes deterministic bytes per file name."""

    def __init__(self, sizes: dict[str, int]) -> None:
        self.sizes = sizes
        self.calls: list[tuple[str, str | None]] = []

    def __call__(
        self, url: str, target: Path, *, token: str | None, expected_size: int | None
    ) -> FileRecord:
        self.calls.append((url, token))
        payload = (target.name.encode() + b"|") * 3
        if expected_size is not None:
            payload = payload.ljust(expected_size, b".")
        target.write_bytes(payload)
        return FileRecord(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())


def _patch_network(mocker) -> _FakeDownloads:
    sizes = {"products.db": 64, "products_drifted.db": 96}
    mocker.patch.dict(CLBENCH_DB_SIZES, sizes, clear=True)
    fake = _FakeDownloads(sizes)
    mocker.patch.object(clbench, "_download", fake)
    return fake


def test_fetch_clbench_writes_files_and_manifest(tmp_path, mocker):
    fake = _patch_network(mocker)
    dest = tmp_path / "data" / "external" / "clbench"

    fetch_clbench(dest, hf_token="hf_secret")

    names = {
        "LICENSE",
        "manifest.json",
        "products.db",
        "products_drifted.db",
        "questions.json",
        "questions_post_drift.json",
        "schema_drift.json",
    }
    assert {p.name for p in dest.iterdir()} == names
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["sha"] == CLBENCH_SHA
    assert manifest["files"]["products.db"] == {"size": 64, "sha256": _sha256(dest / "products.db")}
    assert set(manifest["files"]) == names - {"manifest.json"}
    github_calls = [(url, token) for url, token in fake.calls if "raw.githubusercontent.com" in url]
    hf_calls = [(url, token) for url, token in fake.calls if "huggingface.co" in url]
    assert len(github_calls) == 4 and all(f"/{CLBENCH_SHA}/" in url for url, _ in github_calls)
    assert all(token is None for _, token in github_calls)
    assert len(hf_calls) == 2 and all(token == "hf_secret" for _, token in hf_calls)
    assert all(f"/resolve/{clbench.CLBENCH_HF_REVISION}/" in url for url, _ in hf_calls)


def test_fetch_clbench_skips_files_that_are_already_complete(tmp_path, mocker):
    fake = _patch_network(mocker)
    dest = tmp_path / "clbench"
    fetch_clbench(dest, hf_token=None)
    first_manifest = (dest / "manifest.json").read_text()
    fake.calls.clear()

    fetch_clbench(dest, hf_token=None)

    assert fake.calls == []
    assert (dest / "manifest.json").read_text() == first_manifest


def test_fetch_clbench_refetches_truncated_files(tmp_path, mocker):
    fake = _patch_network(mocker)
    dest = tmp_path / "clbench"
    fetch_clbench(dest, hf_token=None)
    (dest / "products.db").write_bytes(b"truncated")
    (dest / "questions.json").write_bytes(b"x")
    fake.calls.clear()

    fetch_clbench(dest, hf_token=None)

    refetched = sorted(url.rsplit("/", 1)[-1] for url, _ in fake.calls)
    assert refetched == ["products.db", "questions.json"]
    assert (dest / "products.db").stat().st_size == 64


def test_fetch_clbench_rehashes_complete_databases_when_the_manifest_is_lost(tmp_path, mocker):
    fake = _patch_network(mocker)
    dest = tmp_path / "clbench"
    fetch_clbench(dest, hf_token=None)
    (dest / "manifest.json").unlink()
    fake.calls.clear()

    fetch_clbench(dest, hf_token=None)

    manifest = json.loads((dest / "manifest.json").read_text())
    assert not [url for url, _ in fake.calls if "huggingface.co" in url]
    assert manifest["files"]["products_drifted.db"] == {
        "size": 96,
        "sha256": _sha256(dest / "products_drifted.db"),
    }


def _clbench_config(**online: Any) -> ExperimentConfig:
    limits: dict[str, Any] = {"max_queries": 15, "sql_row_limit": 50, "sql_timeout_s": 10.0}
    return ExperimentConfig(
        name="clbench-test",
        phase="test",
        task="clbench",
        arms=["A0"],
        stream_ids=["clbench-r0"],
        seeds=[0],
        sessions=8,
        questions_per_session=5,
        inject_false_notes=False,
        agent_model="m",
        consolidator_model="m",
        embedding_model="e",
        probe_sessions=[],
        budgets=Budgets(phase_cap_usd=1.0, online=OnlineBudget(**(limits | online))),
    )


def test_make_env_accepts_a_clbench_config_stating_the_adapter_limits(tmp_path):
    env, stream = make_env(
        _clbench_config(),
        "clbench-r0",
        0,
        streams_root=tmp_path / "none",
        clbench_root=_make_root(tmp_path),
    )
    assert stream is None and env.max_queries == MAX_QUERIES == 15


@pytest.mark.parametrize(
    "online", [{"max_queries": 8}, {"sql_row_limit": 20}, {"sql_timeout_s": 5.0}]
)
def test_make_env_refuses_clbench_limits_that_differ_from_the_adapter(tmp_path, online):
    with pytest.raises(ValueError, match="clbench-r0"):
        make_env(
            _clbench_config(**online),
            "clbench-r0",
            0,
            streams_root=tmp_path / "none",
            clbench_root=_make_root(tmp_path),
        )


def test_clbench_experiment_config_matches_the_adapter_limits():
    config = load_experiment(
        Path(__file__).resolve().parents[2] / "configs/experiments/clbench.yaml"
    )
    online = config.budgets.online
    assert (online.max_queries, online.sql_row_limit, online.sql_timeout_s) == (
        MAX_QUERIES,
        MAX_RESULT_ROWS,
        SQL_TIMEOUT_S,
    )
