"""Builders for small synthetic run data shared by the evaluation tests."""

import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from project_module.config import ArmName, Budgets, ExperimentConfig
from project_module.contracts import (
    ConsolidationRecord,
    GradeCategory,
    ProbeResult,
    QuestionResult,
    RunStatus,
)
from project_module.evaluation.load import RunData, job_key
from project_module.layout import RunLayout
from project_module.manifest import RunManifest, config_sha256, write_manifest
from project_module.schemadrift.models import Fact, InjectedNote, Question, Stream

LEDGER_SCHEMA = """
CREATE TABLE reservations (
    id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    run_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    session INTEGER NOT NULL,
    component TEXT NOT NULL,
    reserved_usd REAL NOT NULL,
    actual_usd REAL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def make_result(
    stream_id: str = "s1",
    arm: ArmName = "A0",
    session: int = 1,
    qid: str = "q1",
    category: GradeCategory = "correct",
    *,
    ok_queries: int = 2,
    tool_calls: int = 3,
    tool_errors: int = 0,
    drift_dependent: bool = False,
    injected_target: bool = False,
    active_markers: Sequence[str] = (),
    score: float | None = None,
) -> QuestionResult:
    return QuestionResult(
        stream_id=stream_id,
        arm=arm,
        session=session,
        qid=qid,
        category=category,
        score=(1.0 if category == "correct" else 0.0) if score is None else score,
        queries=ok_queries,
        ok_queries=ok_queries,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        turns=3,
        drift_dependent=drift_dependent,
        injected_target=injected_target,
        active_markers=list(active_markers),
    )


def make_probe(
    stream_id: str, arm: ArmName, session: int, fact_id: str, *, correct: bool, injected: bool
) -> ProbeResult:
    return ProbeResult(
        stream_id=stream_id,
        arm=arm,
        session=session,
        fact_id=fact_id,
        injected=injected,
        correct=correct,
    )


def make_consolidation(
    stream_id: str,
    arm: ArmName,
    session: int,
    *,
    markers: Sequence[str] = (),
    lint_problems: Sequence[str] = (),
    stopped: str = "final",
    injected_updated: bool = True,
) -> ConsolidationRecord:
    return ConsolidationRecord(
        stream_id=stream_id,
        arm=arm,
        session=session,
        phases=["organizer"],
        turns=5,
        input_tokens=1000,
        output_tokens=100,
        stopped=stopped,
        lint_problems=list(lint_problems),
        injected_markers_present=list(markers),
        injected_updated=injected_updated,
    )


def cost_row(
    stream_id: str, arm: str, component: str, cost_usd: float, *, session: int = 1
) -> dict[str, Any]:
    return {
        "phase": "pilot",
        "stream_id": stream_id,
        "arm": arm,
        "session": session,
        "component": component,
        "calls": 1,
        "cost_usd": cost_usd,
    }


def make_run(
    *,
    results: Iterable[QuestionResult] = (),
    probes: Iterable[ProbeResult] = (),
    consolidations: Iterable[ConsolidationRecord] = (),
    status: Mapping[str, RunStatus] | None = None,
    costs: Iterable[dict[str, Any]] = (),
    required_jobs: Iterable[str] | None = None,
) -> RunData:
    """Run data; by default every job with results is required and completed."""
    result_list = list(results)
    if status is None:
        status = {job_key(r.stream_id, r.arm): "completed" for r in result_list}
    return RunData(
        required_jobs=sorted(status) if required_jobs is None else list(required_jobs),
        results=result_list,
        probes=list(probes),
        consolidations=list(consolidations),
        status=dict(status),
        costs=list(costs),
    )


def replace_results(
    data: RunData, matches: Callable[[QuestionResult], bool], **changes: Any
) -> RunData:
    """Return a copy of `data` where every result satisfying `matches` is updated."""
    updated = [r.model_copy(update=changes) if matches(r) else r for r in data.results]
    return data.model_copy(update={"results": updated})


def make_stream(
    stream_id: str,
    *,
    injections: Sequence[InjectedNote] = (),
    sessions: int = 12,
    question_facts: Mapping[int, Sequence[str]] | None = None,
    injected_sessions: Collection[int] = (),
) -> Stream:
    """A stream with one question `q<k>` per session and the facts `f1` and `f2`."""
    facts_by_session = question_facts or {}
    questions = [
        Question(
            qid=f"q{k}",
            session=k,
            text="?",
            ordered=False,
            fact_ids=list(facts_by_session.get(k, [])),
            drift_dependent=False,
            injected_target=k in injected_sessions,
            gold_sql_by_epoch={1: "SELECT 1"},
            expected_by_epoch={1: [[1]]},
        )
        for k in range(1, sessions + 1)
    ]
    facts = [
        Fact(
            fact_id=fact_id,
            kind="status_code",
            subject=fact_id,
            value="x",
            valid_from_session=1,
            valid_until_session=None,
            leaf=True,
        )
        for fact_id in ("f1", "f2")
    ]
    return Stream(
        stream_id=stream_id,
        seed=1,
        root=Path("."),
        sessions=sessions,
        questions_per_session=1,
        questions=questions,
        facts=facts,
        injections=list(injections),
    )


MARK_1 = "MARK-1"
MARK_2 = "MARK-2"


def make_injection_stream(stream_id: str = "i1") -> Stream:
    """False notes on f1 after session 3 and on f2 after session 7.

    Sessions 3 and 7 hold the question asked just before each injection; sessions 4 and 8 hold
    the Q_inj questions that depend on the injected fact.
    """
    notes = [
        InjectedNote(
            after_session=3,
            fact_id="f1",
            false_value="9",
            text="note one",
            marker=MARK_1,
            claim_patterns=["f1 is 9"],
        ),
        InjectedNote(
            after_session=7,
            fact_id="f2",
            false_value="9",
            text="note two",
            marker=MARK_2,
            claim_patterns=["f2 is 9"],
        ),
    ]
    return make_stream(
        stream_id,
        injections=notes,
        question_facts={3: ["f1"], 4: ["f1"], 7: ["f2"], 8: ["f2"]},
        injected_sessions=(4, 8),
    )


def _markers_in_memory(arm: ArmName, session: int) -> list[str]:
    """Markers in memory after the consolidation of `session`.

    A1 and A3 never lose a false note; A4 drops the first one at once and keeps the second.
    """
    if arm == "A4":
        return [MARK_2] if session >= 7 else []
    markers = [MARK_1] if session >= 3 else []
    return markers + [MARK_2] if session >= 7 else markers


def make_injection_run(stream_id: str = "i1") -> RunData:
    """Arms A0, A1, A3, A4 on `make_injection_stream`.

    A0 answers everything. A1 and A3 fail both Q_inj questions while the false note is in
    memory. A4 removes the first note and answers its Q_inj, but keeps the second note and fails
    its Q_inj. Only A4 is corrected after the first injection (marker gone, next probe right).
    """
    results: list[QuestionResult] = []
    consolidations: list[ConsolidationRecord] = []
    probes: list[ProbeResult] = []
    for arm in ("A0", "A1", "A3", "A4"):
        for session in range(1, 13):
            markers = [] if arm == "A0" else _markers_in_memory(arm, session - 1)
            failed = session in (4, 8) and (arm in ("A1", "A3") or (arm == "A4" and session == 8))
            results.append(
                make_result(
                    stream_id,
                    arm,
                    session,
                    f"q{session}",
                    "wrong_result" if failed else "correct",
                    injected_target=session in (4, 8),
                    active_markers=markers,
                )
            )
        if arm == "A0":
            continue
        for session in range(1, 12):
            consolidations.append(
                make_consolidation(
                    stream_id, arm, session, markers=_markers_in_memory(arm, session)
                )
            )
        for session, fact_id in ((4, "f1"), (4, "f2"), (8, "f1"), (8, "f2"), (12, "f1")):
            injected = (session, fact_id) in ((4, "f1"), (8, "f2"))
            fixed = arm == "A4" and (session, fact_id) == (4, "f1")
            probes.append(
                make_probe(
                    stream_id,
                    arm,
                    session,
                    fact_id,
                    correct=fixed or not injected,
                    injected=injected,
                )
            )
    return make_run(results=results, consolidations=consolidations, probes=probes)


def write_run_manifest(
    run_dir: Path,
    *,
    arms: Sequence[ArmName],
    stream_ids: Sequence[str],
    arm_overrides: Mapping[str, list[ArmName]] | None = None,
) -> None:
    """Write the `manifest.json` of a pilot run that asked for `stream_ids` x `arms`."""
    config = ExperimentConfig(
        name="pilot",
        phase="pilot",
        arms=list(arms),
        arm_overrides=dict(arm_overrides or {}),
        stream_ids=list(stream_ids),
        seeds=list(range(len(stream_ids))),
        inject_false_notes=False,
        agent_model="m",
        consolidator_model="m",
        embedding_model="e",
        budgets=Budgets(phase_cap_usd=1.0),
    )
    write_manifest(
        RunLayout(run_dir),
        RunManifest(
            run_id=run_dir.name,
            phase="pilot",
            created_utc="2026-11-10T00:00:00+00:00",
            git_sha=None,
            git_dirty=None,
            config_sha256=config_sha256(config),
            config=config,
            input_sha256={},
        ),
    )


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        row.model_dump_json() if hasattr(row, "model_dump_json") else json.dumps(row)
        for row in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ledger(path: Path, rows: Iterable[tuple[Any, ...]], *, wal: bool = False) -> None:
    """Create a ledger file with the `reservations` columns the evaluation code reads.

    Each row is (id, phase, run_id, stream_id, arm, session, component, reserved_usd,
    actual_usd, state).
    """
    connection = sqlite3.connect(path)
    try:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(LEDGER_SCHEMA)
        connection.executemany(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-11-10T00:00:00')",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
