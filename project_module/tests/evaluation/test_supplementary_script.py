"""The post-hoc helpers in `scripts/supplementary_analysis.py`."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from project_module.contracts import QuestionResult
from project_module.evaluation.load import RunData

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "supplementary_analysis.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("supplementary_analysis", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_calls_counts_only_call_lines_per_arm(script: ModuleType, tmp_path: Path) -> None:
    transcripts = tmp_path / "main-01" / "A4" / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "session_001.txt").write_text(
        "USER: q\n"
        'TOOL CALL query: {"sql": "SELECT 1"}\n'
        "TOOL RESULT (query): 1\n"
        'TOOL CALL memory_read: {"path": "wiki/a.md"}\n'
        "TOOL RESULT (memory_read): TOOL CALL query: quoted text\n"
        'TOOL CALL query: {"sql": "SELECT 2"}\n',
        encoding="utf-8",
    )

    assert script._tool_calls(tmp_path, ["main-01"]) == {"A4": {"memory_read": 1, "query": 2}}


def test_tool_calls_skip_streams_outside_the_analysis(script: ModuleType, tmp_path: Path) -> None:
    transcripts = tmp_path / "main-02" / "A4" / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "session_001.txt").write_text('TOOL CALL query: {"sql": "SELECT 1"}\n')

    assert script._tool_calls(tmp_path, ["main-01"]) == {}


@pytest.mark.parametrize(
    ("session", "band"), [(1, "s1-4"), (4, "s1-4"), (5, "s5-8"), (12, "s9-12")]
)
def test_band_maps_sessions_to_migration_epochs(
    script: ModuleType, session: int, band: str
) -> None:
    assert script._band(session) == band


def _result(stream_id: str, arm: str, session: int, *, drift: bool, queries: int) -> QuestionResult:
    return QuestionResult(
        stream_id=stream_id,
        arm=arm,
        session=session,
        qid=f"s{session:02d}q1",
        category="correct",
        score=1.0,
        queries=queries,
        ok_queries=queries,
        tool_calls=queries,
        tool_errors=0,
        turns=queries + 1,
        drift_dependent=drift,
        injected_target=True,
        active_markers=[],
    )


def test_supplementary_drops_streams_outside_the_analysis(
    script: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [
        _result(stream_id, arm, session, drift=drift, queries=3 if stream_id == "main-01" else 99)
        for stream_id in ("main-01", "main-02")
        for arm in ("A0", "A1", "A3", "A4")
        for session in (1, 5, 9)
        for drift in (True, False)
    ]
    data = RunData(
        required_jobs=[], results=results, probes=[], consolidations=[], status={}, costs=[]
    )
    monkeypatch.setattr(script, "load_run", lambda run_dir, ledger: data)
    monkeypatch.setattr(script, "analyzable_streams", lambda data, arms: ["main-01"])

    report = script.supplementary(tmp_path, tmp_path / "ledger.sqlite", [("A4", "A1")])

    assert report["n_streams"] == 1
    assert report["mean_queries_per_question"]["A4"] == {"s1-4": 3, "s5-8": 3, "s9-12": 3}
    assert report["q_inj_outcomes"]["A4"] == {"no_note_detected": {"correct": 6}}
