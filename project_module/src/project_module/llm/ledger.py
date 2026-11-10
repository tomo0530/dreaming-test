"""Process-shared cost ledger: reserve before every API call, settle from `usage` afterwards.

Every write runs inside `BEGIN IMMEDIATE`, so the cap check and the insert of a new
reservation are atomic across threads and processes that share the SQLite file.
"""

import logging
import math
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from project_module.config import ModelSpec, Phase
from project_module.contracts import CostTag, Usage

logger = logging.getLogger(__name__)

INITIAL_SAFETY_FACTOR: Final = 1.25
SAFETY_FACTOR_HEADROOM: Final = 1.05
RESERVATION_OVERHEAD_TOKENS: Final = 64
HARD_CAP_MARGIN_USD: Final = 2.0
BUSY_TIMEOUT_S: Final = 60.0

_MAX_RATIO_KEY: Final = "max_input_ratio"
_PER_MILLION: Final = 1_000_000

ReservationState = Literal["open", "settled", "forfeited", "released"]

_SCHEMA: Final = (
    """
CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    run_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    session INTEGER NOT NULL,
    component TEXT NOT NULL,
    model TEXT NOT NULL,
    model_spec TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    reserved_usd REAL NOT NULL,
    actual_usd REAL,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    overrun INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('open', 'settled', 'forfeited', 'released')),
    created_at TEXT NOT NULL,
    closed_at TEXT
)
""",
    "CREATE INDEX IF NOT EXISTS reservations_phase_run ON reservations (phase, run_id)",
    "CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value REAL NOT NULL)",
)

# Committed cost of one reservation row, as an SQL expression. Settled rows cost what was
# billed; forfeited and open rows may have been billed, so they keep their full reservation;
# released rows were certainly not billed. Every reader of the ledger prices rows with it.
COMMITTED_USD_SQL: Final = """
CASE state
    WHEN 'settled' THEN actual_usd
    WHEN 'released' THEN 0.0
    ELSE reserved_usd
END
"""


class BudgetExceeded(RuntimeError):
    """A reservation would push committed spend over the phase cap or the hard cap."""


def cost_usd(model: ModelSpec, usage: Usage) -> float:
    """Price `usage`: cached input at the cached rate, the rest of the input at the input rate."""
    if usage.cached_input_tokens > usage.input_tokens:
        raise ValueError("cached_input_tokens exceeds input_tokens")
    uncached = usage.input_tokens - usage.cached_input_tokens
    return (
        uncached * model.input_usd_per_m
        + usage.cached_input_tokens * model.cached_input_usd_per_m
        + usage.output_tokens * model.output_usd_per_m
    ) / _PER_MILLION


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CostLedger:
    """Reservation ledger shared by every process that points at the same SQLite file.

    Committed spend is settled actual cost plus the full reservation of every forfeited
    (billing unknown) and open (in flight, or orphaned by a crash) row. Open rows are never
    released on restart: a process that died mid-call may still have been billed.

    Parameters
    ----------
    path
        SQLite file; created with its parent directory if missing.
    phase
        Phase this ledger reserves for. Every tag passed to `reserve` must carry it.
    phase_cap_usd
        Upper bound on committed spend of `phase`.
    hard_cap_usd
        Upper bound on committed spend of all phases. The check uses
        `hard_cap_usd - HARD_CAP_MARGIN_USD`.
    """

    def __init__(
        self, path: Path, *, phase: Phase, phase_cap_usd: float, hard_cap_usd: float
    ) -> None:
        # NaN compares false with everything, so a NaN cap would never refuse a reservation.
        if not math.isfinite(phase_cap_usd) or phase_cap_usd <= 0:
            raise ValueError("phase_cap_usd must be a finite positive number")
        if not math.isfinite(hard_cap_usd):
            raise ValueError("hard_cap_usd must be a finite number")
        if hard_cap_usd <= HARD_CAP_MARGIN_USD:
            raise ValueError(
                f"hard_cap_usd must exceed the ${HARD_CAP_MARGIN_USD:.2f} safety margin"
            )
        self._path = path
        self._phase: Phase = phase
        self._phase_cap_usd = phase_cap_usd
        self._effective_hard_cap_usd = hard_cap_usd - HARD_CAP_MARGIN_USD
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)

    @property
    def phase(self) -> Phase:
        return self._phase

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _safety_factor(conn: sqlite3.Connection) -> float:
        row = conn.execute(
            "SELECT value FROM ledger_meta WHERE key = ?", (_MAX_RATIO_KEY,)
        ).fetchone()
        if row is None:
            return INITIAL_SAFETY_FACTOR
        return max(INITIAL_SAFETY_FACTOR, float(row["value"]) * SAFETY_FACTOR_HEADROOM)

    @staticmethod
    def _committed(conn: sqlite3.Connection, *, phase: Phase | None) -> float:
        query = f"SELECT COALESCE(SUM({COMMITTED_USD_SQL}), 0.0) FROM reservations"
        if phase is None:
            return float(conn.execute(query).fetchone()[0])
        return float(conn.execute(query + " WHERE phase = ?", (phase,)).fetchone()[0])

    def reserve(
        self, model: ModelSpec, *, input_tokens: int, max_output_tokens: int, tag: CostTag
    ) -> str:
        """Reserve the worst-case cost of one call and return the reservation id.

        Raises
        ------
        BudgetExceeded
            If the reservation would exceed the phase cap or the effective hard cap.
        ValueError
            If the token counts are negative or `tag.phase` differs from the ledger's phase.
        """
        if input_tokens < 0 or max_output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        if tag.phase != self._phase:
            raise ValueError(f"tag phase {tag.phase!r} does not match ledger phase {self._phase!r}")
        rid = uuid.uuid4().hex
        with self._transaction() as conn:
            factor = self._safety_factor(conn)
            reserved = (
                (input_tokens * factor + RESERVATION_OVERHEAD_TOKENS) * model.input_usd_per_m
                + max_output_tokens * model.output_usd_per_m
            ) / _PER_MILLION
            total = self._committed(conn, phase=None) + reserved
            if total > self._effective_hard_cap_usd:
                raise BudgetExceeded(
                    f"reserving ${reserved:.4f} would bring total spend to ${total:.4f}, over the "
                    f"hard cap (${self._effective_hard_cap_usd:.2f} after the safety margin)"
                )
            phase_total = self._committed(conn, phase=self._phase) + reserved
            if phase_total > self._phase_cap_usd:
                raise BudgetExceeded(
                    f"reserving ${reserved:.4f} would bring phase {self._phase!r} spend to "
                    f"${phase_total:.4f}, over its cap of ${self._phase_cap_usd:.2f}"
                )
            conn.execute(
                "INSERT INTO reservations (id, phase, run_id, stream_id, arm, session, component,"
                " model, model_spec, estimated_input_tokens, reserved_usd, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (
                    rid,
                    tag.phase,
                    tag.run_id,
                    tag.stream_id,
                    tag.arm,
                    tag.session,
                    tag.component,
                    model.deployment,
                    model.model_dump_json(),
                    input_tokens,
                    reserved,
                    _now(),
                ),
            )
        return rid

    def _open_row(self, conn: sqlite3.Connection, rid: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
        if row is None:
            raise ValueError(f"unknown reservation {rid}")
        if row["state"] != "open":
            raise ValueError(f"reservation {rid} is {row['state']}, not open")
        return row  # type: ignore[no-any-return]

    def settle(self, rid: str, usage: Usage) -> float:
        """Replace the reservation with the billed cost and return that cost in USD."""
        with self._transaction() as conn:
            row = self._open_row(conn, rid)
            actual = cost_usd(ModelSpec.model_validate_json(row["model_spec"]), usage)
            overrun = actual > row["reserved_usd"]
            conn.execute(
                "UPDATE reservations SET state = 'settled', actual_usd = ?, input_tokens = ?,"
                " cached_input_tokens = ?, output_tokens = ?, overrun = ?, closed_at = ?"
                " WHERE id = ?",
                (
                    actual,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    int(overrun),
                    _now(),
                    rid,
                ),
            )
            estimated = row["estimated_input_tokens"]
            if estimated > 0:
                conn.execute(
                    "INSERT INTO ledger_meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = MAX(value, excluded.value)",
                    (_MAX_RATIO_KEY, usage.input_tokens / estimated),
                )
        if overrun:
            logger.warning(
                "Reservation %s overran: actual $%.6f > reserved $%.6f (input %d vs estimate %d)",
                rid,
                actual,
                row["reserved_usd"],
                usage.input_tokens,
                estimated,
            )
        return actual

    def _close(self, rid: str, state: ReservationState) -> None:
        with self._transaction() as conn:
            self._open_row(conn, rid)
            conn.execute(
                "UPDATE reservations SET state = ?, closed_at = ? WHERE id = ?",
                (state, _now(), rid),
            )

    def forfeit(self, rid: str) -> None:
        """Billing is unknown: keep the full reservation as spent."""
        self._close(rid, "forfeited")
        logger.warning("Reservation %s forfeited (billing unknown)", rid)

    def release(self, rid: str) -> None:
        """The call was certainly not billed: free the reservation."""
        self._close(rid, "released")

    def spent_usd(self, *, phase: Phase | None = None) -> float:
        """Committed spend (settled + forfeited + open), for one phase or all phases."""
        conn = self._connect()
        try:
            return self._committed(conn, phase=phase)
        finally:
            conn.close()

    def cost_by(self, *, phase: Phase, run_id: str) -> list[dict[str, Any]]:
        """Committed spend and token totals of one run, grouped by the tag columns.

        Each row has `stream_id`, `arm`, `session`, `component`, `calls` (non-released
        reservations), `usd` (committed), `input_tokens`, `cached_input_tokens` and
        `output_tokens` (settled usage only).
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT stream_id, arm, session, component, COUNT(*) AS calls,"
                f" SUM({COMMITTED_USD_SQL}) AS usd,"
                " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                " COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,"
                " COALESCE(SUM(output_tokens), 0) AS output_tokens"
                " FROM reservations WHERE phase = ? AND run_id = ? AND state != 'released'"
                " GROUP BY stream_id, arm, session, component"
                " ORDER BY stream_id, arm, session, component",
                (phase, run_id),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]
