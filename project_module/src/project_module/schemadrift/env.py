"""`TaskEnv` for SchemaDrift-SQL: serves a stream's questions and epoch databases."""

import copy
from typing import Any

from project_module.contracts import Grade, TaskQuestion
from project_module.schemadrift.grader import grade_submission
from project_module.schemadrift.models import Question, Stream
from project_module.schemadrift.sql import SqlError, SqlResult, run_sql_readonly

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
    """

    submit_param = "sql"

    def __init__(
        self, stream: Stream, *, row_limit: int, timeout_s: float, max_queries: int
    ) -> None:
        self.stream = stream
        self.env_id = stream.stream_id
        self.sessions = stream.sessions
        self.max_queries = max_queries
        self._row_limit = row_limit
        self._timeout_s = timeout_s
        self._questions: dict[str, Question] = {q.qid: q for q in stream.questions}

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
        return None
