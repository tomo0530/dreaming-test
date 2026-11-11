"""`TaskEnv` for SchemaDrift-SQL: serves a stream's questions and epoch databases."""

import copy
from typing import Any

from project_module.contracts import Grade, TaskQuestion
from project_module.schemadrift.grader import grade_submission
from project_module.schemadrift.models import Question, Stream
from project_module.schemadrift.sql import SqlError, SqlResult, run_sql_readonly

# Caps on the expected result shown in graded feedback; the block incl. its marker stays within.
FEEDBACK_MAX_ROWS = 20
FEEDBACK_MAX_CHARS = 2000
_TRUNCATION_MARK = "[truncated]"

_SUBMIT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_sql",
        "description": (
            "Submit your final answer: one read-only SQL query whose full result answers the "
            "question. It runs against the current database. Call this once, when you are done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The final SQL query."},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    return str(value).replace("\t", "\\t").replace("\n", "\\n")


def _format_result(result: SqlResult, row_limit: int) -> str:
    """Render a result as a tab-separated header line followed by one line per row."""
    lines = ["\t".join(result.columns)]
    lines += ["\t".join(_cell(value) for value in row) for row in result.rows]
    if not result.rows:
        lines.append("(0 rows)")
    if result.truncated:
        lines.append(f"... (truncated to {row_limit} rows)")
    return "\n".join(lines)


def _render_expected(rows: list[list[Any]]) -> str:
    """One line per expected row, cells joined by " | ", within the feedback caps.

    Rows past `FEEDBACK_MAX_ROWS`, or past the character cap, are dropped whole and a
    `[truncated]` line follows; a single row that alone exceeds the cap is cut mid-row.
    """
    if not rows:
        return "(0 rows)"
    lines = [" | ".join(_cell(value) for value in row) for row in rows[:FEEDBACK_MAX_ROWS]]
    complete = len(rows) <= FEEDBACK_MAX_ROWS
    text = "\n".join(lines)
    if complete and len(text) <= FEEDBACK_MAX_CHARS:
        return text
    room = FEEDBACK_MAX_CHARS - len(_TRUNCATION_MARK) - 1
    kept: list[str] = []
    used = 0
    for line in lines:
        used += len(line) + (1 if kept else 0)
        if used > room:
            break
        kept.append(line)
    if not kept:
        kept = [lines[0][:room]]
    return "\n".join([*kept, _TRUNCATION_MARK])


class SchemaDriftEnv:
    """Runs exploration queries on the current epoch's database and grades submitted SQL.

    Parameters
    ----------
    stream
        The stream to serve; its epoch databases must exist under `stream.root`.
    row_limit, timeout_s
        Limits for exploration queries (grading uses its own, larger limits).
    max_queries
        Exposed for the session runner, which enforces it.
    feedback
        Whether `feedback` reports each grade and the expected result (the `graded` runs).
    """

    submit_param = "sql"

    def __init__(
        self,
        stream: Stream,
        *,
        row_limit: int,
        timeout_s: float,
        max_queries: int,
        feedback: bool = False,
    ) -> None:
        self.stream = stream
        self.env_id = stream.stream_id
        self.sessions = stream.sessions
        self.max_queries = max_queries
        self._row_limit = row_limit
        self._timeout_s = timeout_s
        self._feedback = feedback
        self._questions: dict[str, Question] = {q.qid: q for q in stream.questions}
        # Numbered in the order the stream presents them: by session, then within a session.
        presented = sorted(stream.questions, key=lambda q: q.session)
        self._numbers = {q.qid: number for number, q in enumerate(presented, start=1)}

    def questions_for(self, session: int) -> list[TaskQuestion]:
        return [
            TaskQuestion(
                qid=q.qid,
                session=q.session,
                text=q.text,
                drift_dependent=q.drift_dependent,
                injected_target=q.injected_target,
            )
            for q in self.stream.questions_for(session)
        ]

    def run_query(self, session: int, sql: str) -> str:
        """Run one exploration query; failures come back as "ERROR: ..." text."""
        if not 1 <= session <= self.sessions:
            raise ValueError(f"session {session} is outside 1..{self.sessions}")
        db_path = self.stream.db_path(self.stream.epoch_of(session))
        try:
            result = run_sql_readonly(
                db_path, sql, row_limit=self._row_limit, timeout_s=self._timeout_s
            )
        except SqlError as err:
            return f"ERROR: {err}"
        return _format_result(result, self._row_limit)

    def submit_tool(self) -> dict[str, Any]:
        return copy.deepcopy(_SUBMIT_TOOL)

    def grade(self, q: TaskQuestion, submission: str | None, queries_used: int) -> Grade:
        return grade_submission(self.stream, self._questions[q.qid], submission)

    def feedback(self, q: TaskQuestion, grade: Grade) -> str | None:
        """The grade line, with the current epoch's expected result unless correct.

        Wording follows `CLBenchEnv.feedback`. None when the env was built without feedback.
        """
        if not self._feedback:
            return None
        question = self._questions[q.qid]
        header = f"Question {self._numbers[q.qid]}:"
        if grade.category == "correct":
            return f"{header} CORRECT!"
        expected = _render_expected(question.expected_by_epoch[self.stream.epoch_of(q.session)])
        if grade.category == "no_submission":
            return (
                f"{header} No answer was submitted within the limits — treated as incorrect.\n"
                f"The correct answer was:\n{expected}"
            )
        return f"{header} INCORRECT.\nCorrect answer:\n{expected}"
