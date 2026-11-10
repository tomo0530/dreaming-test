"""Thin adapter that reproduces CL-Bench's ``database_exploration`` task (``schema_drift`` variant).

CL-Bench (Apache-2.0, ``pgasawa/continual-learning-bench``) cannot be imported because of its
pinned dependencies, so the rules of the official ``task.py`` / ``prompts.py`` at ``CLBENCH_SHA``
are re-implemented here: the question order, the allowed SQL, the 50-row cap, the 10 s timeout,
the 15-query budget, answer matching, the reward, the feedback text and the drift notice.

Known differences from the official run loop, all forced by the ``TaskEnv`` contract:

* ``run_query`` is stateless (it opens a fresh read-only connection per call), so the
  ``Query result (n/15 queries used, ...)`` header is not produced; counting queries and refusing
  the 16th is the session runner's job.
* ``feedback`` receives only the question and the grade, so the ``Your answer`` and
  ``Exploratory queries used`` lines of the official feedback are not reproduced. The wording of
  the official ``TIMED OUT`` / ``budget exceeded`` variants is merged into one ``no_submission``
  message because the grade does not say which of the two happened.
* The 50-row cap also applies to ``PRAGMA`` output (the official code only states it for queries).
"""

import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import time
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, TypeAdapter

from project_module.contracts import Grade, TaskEnv, TaskQuestion

logger = logging.getLogger(__name__)

CLBENCH_SHA = "5f8c50eb1e84b2eda2ef4faff757dfc812a0ea26"
CLBENCH_HF_REPO = "continual-learning-bench/database-exploration"
CLBENCH_HF_REVISION = "a0cc57eeb9a54f01c0490a1b46cb705b4e05aa19"
CLBENCH_DB_SIZES = {"products.db": 397_910_016, "products_drifted.db": 432_148_480}

MANIFEST_NAME = "manifest.json"
_GITHUB_RAW = f"https://raw.githubusercontent.com/pgasawa/continual-learning-bench/{CLBENCH_SHA}"
# Local file name -> path inside the pinned repository.
_GITHUB_FILES = {
    "questions.json": "data/database_exploration/questions.json",
    "questions_post_drift.json": "data/database_exploration/questions_post_drift.json",
    "schema_drift.json": "src/tasks/database_exploration/variants/schema_drift.json",
    "LICENSE": "LICENSE",
}
_HF_RESOLVE = f"https://huggingface.co/datasets/{CLBENCH_HF_REPO}/resolve/{CLBENCH_HF_REVISION}"
_DOWNLOAD_CHUNK_BYTES = 1 << 20
_DOWNLOAD_TIMEOUT_S = 60.0

# Task constants of the ``schema_drift`` variant and ``default`` schedule.
PRE_DRIFT_COUNT = 20
POST_DRIFT_COUNT = 20
MAX_QUERIES = 15
MAX_RESULT_ROWS = 50
SQL_TIMEOUT_S = 10.0
_SEED = 42
_PRE_SHUFFLE_OFFSET = 410_917
_POST_SHUFFLE_OFFSET = 910_357
_RUN_STRIDE = 100_003

DRIFT_NOTICE = (
    "NOTICE: The live database schema or contents may have changed since your earlier "
    "exploration. Inspect current metadata and data before relying on prior assumptions."
)

_QUESTION_TEMPLATE = """\
Question {number}/{total}

{question}

You have access to a SQLite database that contains three separate product datasets covering \
different categories: office products, electronics, and musical instruments. Each dataset has \
its own set of tables. You will need to explore the schema to determine which tables belong to \
which product category.

You may run up to {budget} exploratory SQL queries to examine the schema and data before \
submitting your final answer. Note: query results are capped at {row_cap} rows — if a query \
would return more, only the first {row_cap} rows are shown. Use aggregates, LIMIT, or filters \
when you need to inspect larger tables.

Tools:
  query — run a SQL statement (SELECT, .tables, PRAGMA, etc.)
  submit_answer — submit your final answer as the exact value (e.g. '50000', '3.14', \
'Acme Corp'). Do NOT describe how to get the answer; provide the actual result."""

_SUBMIT_DESCRIPTION = (
    "Submit your final answer: the exact value that answers the question — a raw number "
    "(e.g. '50000', '3.14') or short text (e.g. 'Acme Corp'). "
    "Do NOT describe how to get the answer; provide the actual value."
)

_INTEGER_PATTERN = re.compile(r"-?\d+")
_NUMBER_PATTERN = re.compile(r"-?\d+\.?\d*")
_READ_ONLY_FIRST_WORDS = ("SELECT", "WITH", "EXPLAIN")
_DISALLOWED_STATEMENT = (
    "ERROR: Only SELECT / WITH / EXPLAIN / PRAGMA statements are allowed. Got: {}"
)


# --- scoring ----------------------------------------------------------------------------------


def evaluate_answer(
    answer: str,
    expected: Any,
    answer_type: Literal["integer", "float", "text"],
    tolerance: float,
) -> bool:
    """Official answer matching (``_evaluate_answer``).

    Integer answers compare the first integer found in ``answer``; float answers compare the
    first number within ``max(tolerance, 1% of expected)``; text answers compare after
    stripping and lower-casing. A blank answer is never correct.
    """
    submitted = answer.strip()
    if not submitted:
        return False
    truth = str(expected).strip()
    try:
        if answer_type == "integer":
            match = _INTEGER_PATTERN.search(submitted)
            return match is not None and int(match.group()) == int(truth)
        if answer_type == "float":
            match = _NUMBER_PATTERN.search(submitted)
            if match is None:
                return False
            expected_value = float(truth)
            return abs(float(match.group()) - expected_value) <= max(
                tolerance, abs(expected_value) * 0.01
            )
    except ValueError:
        return False
    return submitted.lower() == truth.lower()


def reward(correct: bool, queries: int, limit: int = MAX_QUERIES) -> float:
    """Official per-question reward: ``1 - queries / limit`` when correct, otherwise 0."""
    return round(1 - queries / limit, 4) if correct else 0.0


# --- read-only SQL ------------------------------------------------------------------------------


def _clean_cell(value: object) -> str:
    if value is None:
        return "NULL"
    return str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")


def _format_table(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    cells = [[_clean_cell(value) for value in row] for row in rows]
    widths = [
        max([len(column), *(len(row[i]) for row in cells)]) for i, column in enumerate(columns)
    ]
    header = " | ".join(column.ljust(width) for column, width in zip(columns, widths, strict=True))
    separator = "-+-".join("-" * width for width in widths)
    body = (" | ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)) for row in cells)
    return "\n".join([header, separator, *body])


def _run_meta_command(conn: sqlite3.Connection, sql: str) -> str | None:
    """Handle ``.tables`` and ``.schema [table]``; None if ``sql`` is not a meta-command."""
    lowered = sql.lower()
    if lowered == ".tables":
        names = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return "\n".join(row[0] for row in names.fetchall())
    if not lowered.startswith(".schema"):
        return None
    parts = sql.split(maxsplit=1)
    if len(parts) > 1:
        found = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (parts[1].strip(),))
    else:
        found = conn.execute("SELECT sql FROM sqlite_master WHERE type='table'")
    return "\n\n".join(row[0] for row in found.fetchall() if row[0])


def _run_statement(conn: sqlite3.Connection, sql: str) -> str:
    meta_result = _run_meta_command(conn, sql)
    if meta_result is not None:
        return meta_result

    words = sql.split()
    first_word = words[0].upper() if words else ""
    if not sql.upper().startswith("PRAGMA") and first_word not in _READ_ONLY_FIRST_WORDS:
        return _DISALLOWED_STATEMENT.format(first_word)

    cursor = conn.execute(sql)
    rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
    if cursor.description is None or not rows:
        return "(no results)"
    columns = [column[0] for column in cursor.description]
    table = _format_table(columns, rows[:MAX_RESULT_ROWS])
    if len(rows) > MAX_RESULT_ROWS:
        table += f"\n... (showing first {MAX_RESULT_ROWS} rows)"
    return table


def _execute_sql(db_path: Path, sql: str) -> str:
    """Run one statement against a read-only connection and return the text shown to the model."""
    statement = sql.strip().rstrip(";").strip()
    deadline = time.monotonic() + SQL_TIMEOUT_S
    # mode=ro makes the database immutable for this connection, so even a ``WITH ... DELETE``
    # cannot modify the shared files.
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            return _run_statement(conn, statement)
    except (sqlite3.Error, ValueError) as error:
        if "interrupted" in str(error).lower():
            return f"ERROR: query exceeded {SQL_TIMEOUT_S:g}s timeout"
        return f"ERROR: {error}"


# --- environment ------------------------------------------------------------------------------


class _RawQuestion(BaseModel):
    question_id: int
    question: str
    answer: int | float | str
    answer_type: Literal["integer", "float", "text"] = "text"
    tolerance: float = 0.0


def _require_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found; run scripts/fetch_clbench.py (fetch_clbench) first"
        )
    return path


def _load_questions(path: Path, count: int) -> list[_RawQuestion]:
    questions = TypeAdapter(list[_RawQuestion]).validate_json(_require_file(path).read_bytes())
    if len(questions) < count:
        raise ValueError(
            f"{path.name} must contain at least {count} questions, found {len(questions)}"
        )
    return questions[:count]


def _shuffled(questions: list[_RawQuestion], seed: int) -> list[_RawQuestion]:
    ordered = list(questions)
    random.Random(seed).shuffle(ordered)
    return ordered


class CLBenchEnv(TaskEnv):
    """CL-Bench ``database_exploration`` / ``schema_drift`` as a ``TaskEnv``.

    The 20 pre-drift questions and the 20 post-drift questions are each shuffled with the
    official per-run seeds, then split into sessions. Sessions after the drift boundary query
    ``products_drifted.db``.

    Parameters
    ----------
    root : Path
        Directory populated by ``fetch_clbench``.
    run_index : int
        Repetition index; selects the official permutation of the questions.
    questions_per_session : int
        Must divide 20 so that the drift boundary falls between two sessions.

    Raises
    ------
    FileNotFoundError
        If a question file or database is missing.
    ValueError
        For an invalid ``run_index`` / ``questions_per_session`` or malformed question files.
    """

    def __init__(self, root: Path, *, run_index: int, questions_per_session: int = 5) -> None:
        if run_index < 0:
            raise ValueError(f"run_index must be >= 0, got {run_index}")
        if questions_per_session < 1 or PRE_DRIFT_COUNT % questions_per_session:
            raise ValueError(
                f"questions_per_session must divide {PRE_DRIFT_COUNT}, got {questions_per_session}"
            )
        self._pre_drift_db = _require_file(root / "products.db")
        self._post_drift_db = _require_file(root / "products_drifted.db")
        pre = _load_questions(root / "questions.json", PRE_DRIFT_COUNT)
        post = _load_questions(root / "questions_post_drift.json", POST_DRIFT_COUNT)
        run_offset = run_index * _RUN_STRIDE
        ordered = [
            *_shuffled(pre, _SEED + _PRE_SHUFFLE_OFFSET + run_offset),
            *_shuffled(post, _SEED + _POST_SHUFFLE_OFFSET + run_offset),
        ]
        if len({raw.question_id for raw in ordered}) != len(ordered):
            raise ValueError("duplicate question_id across the pre- and post-drift question files")

        self.env_id = f"clbench-r{run_index}"
        self.sessions = len(ordered) // questions_per_session
        self.max_queries = MAX_QUERIES
        self.submit_param = "value"
        self._questions_per_session = questions_per_session
        self._first_drift_session = PRE_DRIFT_COUNT // questions_per_session + 1
        self._questions = [
            self._to_task_question(raw, index, len(ordered)) for index, raw in enumerate(ordered)
        ]
        self._raw = {f"q{raw.question_id}": raw for raw in ordered}
        self._numbers = {question.qid: index + 1 for index, question in enumerate(self._questions)}

    def _to_task_question(self, raw: _RawQuestion, index: int, total: int) -> TaskQuestion:
        drifted = index >= PRE_DRIFT_COUNT
        text = _QUESTION_TEMPLATE.format(
            number=index + 1,
            total=total,
            question=raw.question,
            budget=MAX_QUERIES,
            row_cap=MAX_RESULT_ROWS,
        )
        if index == PRE_DRIFT_COUNT:
            text = f"{DRIFT_NOTICE}\n\n{text}"
        return TaskQuestion(
            qid=f"q{raw.question_id}",
            session=index // self._questions_per_session + 1,
            text=text,
            drift_dependent=drifted,
            injected_target=False,
        )

    def _check_session(self, session: int) -> None:
        if not 1 <= session <= self.sessions:
            raise ValueError(f"session must be in 1..{self.sessions}, got {session}")

    def _record(self, question: TaskQuestion) -> _RawQuestion:
        if question.qid not in self._raw:
            raise ValueError(f"unknown question: {question.qid}")
        return self._raw[question.qid]

    def questions_for(self, session: int) -> list[TaskQuestion]:
        self._check_session(session)
        start = (session - 1) * self._questions_per_session
        return self._questions[start : start + self._questions_per_session]

    def run_query(self, session: int, sql: str) -> str:
        self._check_session(session)
        drifted = session >= self._first_drift_session
        return _execute_sql(self._post_drift_db if drifted else self._pre_drift_db, sql)

    def submit_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "submit_answer",
                "description": _SUBMIT_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string", "description": _SUBMIT_DESCRIPTION}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }

    def grade(self, q: TaskQuestion, submission: str | None, queries_used: int) -> Grade:
        raw = self._record(q)
        if submission is None or queries_used > self.max_queries:
            return Grade(category="no_submission", score=0.0)
        if not evaluate_answer(submission, raw.answer, raw.answer_type, raw.tolerance):
            return Grade(category="wrong_result", score=0.0)
        return Grade(category="correct", score=reward(True, queries_used, self.max_queries))

    def feedback(self, q: TaskQuestion, grade: Grade) -> str | None:
        raw = self._record(q)
        header = f"Question {self._numbers[q.qid]}:"
        if grade.category == "correct":
            return f"{header} CORRECT!"
        if grade.category == "no_submission":
            return (
                f"{header} No answer was submitted within the limits — treated as incorrect.\n"
                f"The correct answer was: {raw.answer}"
            )
        return f"{header} INCORRECT.\nCorrect answer: {raw.answer}"


# --- fetch ------------------------------------------------------------------------------------


class FileRecord(BaseModel):
    size: int
    sha256: str


class _Manifest(BaseModel):
    sha: str = CLBENCH_SHA
    hf_repo: str = CLBENCH_HF_REPO
    hf_revision: str = CLBENCH_HF_REVISION
    files: dict[str, FileRecord]


@dataclass(frozen=True)
class _Artifact:
    name: str
    url: str
    expected_size: int | None
    from_hugging_face: bool


def _artifacts() -> list[_Artifact]:
    from_github = [
        _Artifact(name, f"{_GITHUB_RAW}/{path}", None, False)
        for name, path in _GITHUB_FILES.items()
    ]
    from_hf = [
        _Artifact(name, f"{_HF_RESOLVE}/{name}", size, True)
        for name, size in CLBENCH_DB_SIZES.items()
    ]
    return [*from_github, *from_hf]


class _DropAuthorizationAcrossHosts(urllib.request.HTTPRedirectHandler):
    """Hugging Face redirects to a signed CDN URL; the bearer token must not follow it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


def _download(
    url: str, target: Path, *, token: str | None, expected_size: int | None
) -> FileRecord:
    """Stream ``url`` to ``target`` atomically, verifying ``expected_size`` when given."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.part")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_DropAuthorizationAcrossHosts)
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            opener.open(request, timeout=_DOWNLOAD_TIMEOUT_S) as response,
            partial.open("wb") as out,
        ):
            while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if expected_size is not None and size != expected_size:
            raise ValueError(f"{target.name}: downloaded size {size} != expected {expected_size}")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return FileRecord(size=size, sha256=digest.hexdigest())


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest_files(path: Path) -> dict[str, FileRecord]:
    if not path.is_file():
        return {}
    return _Manifest.model_validate_json(path.read_bytes()).files


def _reuse(
    target: Path, expected_size: int | None, previous: FileRecord | None
) -> FileRecord | None:
    """Return the record of an already fetched file, or None if it must be downloaded again."""
    if not target.is_file():
        return None
    size = target.stat().st_size
    known_size = (
        expected_size if expected_size is not None else (previous.size if previous else None)
    )
    if size != known_size:
        return None
    if previous is not None and previous.size == size:
        return previous
    return FileRecord(size=size, sha256=_sha256_of(target))


def fetch_clbench(dest: Path, *, hf_token: str | None) -> None:
    """Download the pinned CL-Bench files into ``dest`` and write ``dest/manifest.json``.

    Files already present with the expected size (databases) or the size recorded in the
    manifest (small files) are not downloaded again. ``hf_token`` is sent to Hugging Face only.

    Raises
    ------
    urllib.error.HTTPError
        If a download is refused (for example a missing or invalid token).
    ValueError
        If a downloaded file does not have the expected size.
    """
    dest.mkdir(parents=True, exist_ok=True)
    previous = _read_manifest_files(dest / MANIFEST_NAME)
    files: dict[str, FileRecord] = {}
    for artifact in _artifacts():
        target = dest / artifact.name
        record = _reuse(target, artifact.expected_size, previous.get(artifact.name))
        if record is None:
            logger.info("Downloading %s", artifact.name)
            token = hf_token if artifact.from_hugging_face else None
            record = _download(
                artifact.url, target, token=token, expected_size=artifact.expected_size
            )
        else:
            logger.info("Skipping %s (already fetched)", artifact.name)
        files[artifact.name] = record
    manifest = _Manifest(files=files)
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest.model_dump(), indent=2, sort_keys=True) + "\n"
    )
