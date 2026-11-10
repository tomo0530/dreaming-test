import multiprocessing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.queues import Queue as QueueType
from multiprocessing.synchronize import Event as EventType
from pathlib import Path

import pytest
from fakes import make_tag
from project_module.config import ModelSpec
from project_module.contracts import Usage
from project_module.llm.ledger import BudgetExceeded, CostLedger, cost_usd

LUNA = ModelSpec(
    deployment="gpt-6-luna",
    input_usd_per_m=0.10,
    cached_input_usd_per_m=0.01,
    output_usd_per_m=0.50,
    kind="chat",
)
# Reserving max_output_tokens=1 with input_tokens=0 costs exactly 64 * 0 + 1 * 0.125 = $0.125,
# a binary-exact amount so cap arithmetic has no rounding noise.
EIGHTH = ModelSpec(
    deployment="eighth",
    input_usd_per_m=0.0,
    cached_input_usd_per_m=0.0,
    output_usd_per_m=125_000.0,
    kind="chat",
)


def _ledger(path: Path, *, phase_cap_usd: float = 100.0, hard_cap_usd: float = 200.0) -> CostLedger:
    return CostLedger(path, phase="test", phase_cap_usd=phase_cap_usd, hard_cap_usd=hard_cap_usd)


def _rows(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM reservations ORDER BY created_at, rowid").fetchall()
    finally:
        conn.close()


def _reserve_eighth(ledger: CostLedger, **tag: object) -> str:
    return ledger.reserve(EIGHTH, input_tokens=0, max_output_tokens=1, tag=make_tag(**tag))


def _reserve_until_refused(path: Path, start: EventType, granted: QueueType[int]) -> None:
    ledger = _ledger(path, hard_cap_usd=3.0)
    start.wait()
    count = 0
    while True:
        try:
            _reserve_eighth(ledger)
        except BudgetExceeded:
            granted.put(count)
            return
        count += 1


def test_cost_usd_splits_cached_tokens() -> None:
    usage = Usage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=100_000)

    cost = cost_usd(LUNA, usage)

    # 600k uncached * $0.10/M + 400k cached * $0.01/M + 100k out * $0.50/M
    assert cost == pytest.approx(0.06 + 0.004 + 0.05)


def test_cost_usd_rejects_more_cached_than_input() -> None:
    with pytest.raises(ValueError, match="cached_input_tokens exceeds input_tokens"):
        cost_usd(LUNA, Usage(input_tokens=10, cached_input_tokens=11, output_tokens=0))


def test_reserve_amount_applies_initial_safety_factor_and_overhead(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite")

    ledger.reserve(LUNA, input_tokens=10_000, max_output_tokens=1_000, tag=make_tag())

    # (10_000 * 1.25 + 64) * $0.10/M + 1_000 * $0.50/M
    [row] = _rows(tmp_path / "ledger.sqlite")
    assert row["reserved_usd"] == pytest.approx(12_564 * 0.10e-6 + 1_000 * 0.50e-6)
    assert row["state"] == "open"


def test_reserve_refuses_over_phase_cap(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite", phase_cap_usd=0.25)
    _reserve_eighth(ledger)
    _reserve_eighth(ledger)

    with pytest.raises(BudgetExceeded, match="phase 'test'"):
        _reserve_eighth(ledger)

    assert len(_rows(tmp_path / "ledger.sqlite")) == 2


def test_reserve_refuses_over_hard_cap_across_phases(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    # The hard cap check uses 2.25 - 2.00 safety margin = $0.25, i.e. two reservations.
    dev = CostLedger(path, phase="dev", phase_cap_usd=100.0, hard_cap_usd=2.25)
    dev.reserve(EIGHTH, input_tokens=0, max_output_tokens=1, tag=make_tag(phase="dev"))
    pilot = CostLedger(path, phase="pilot", phase_cap_usd=100.0, hard_cap_usd=2.25)
    pilot.reserve(EIGHTH, input_tokens=0, max_output_tokens=1, tag=make_tag(phase="pilot"))

    with pytest.raises(BudgetExceeded, match="hard cap"):
        pilot.reserve(EIGHTH, input_tokens=0, max_output_tokens=1, tag=make_tag(phase="pilot"))

    assert len(_rows(path)) == 2


def test_reserve_rejects_tag_from_another_phase(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite")

    with pytest.raises(ValueError, match="tag phase 'dev' does not match ledger phase 'test'"):
        ledger.reserve(LUNA, input_tokens=1, max_output_tokens=1, tag=make_tag(phase="dev"))


def test_ledger_rejects_hard_cap_not_above_safety_margin(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hard_cap_usd must exceed"):
        _ledger(tmp_path / "ledger.sqlite", hard_cap_usd=2.0)


def test_settle_replaces_reservation_with_actual(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = _ledger(path)
    rid = ledger.reserve(LUNA, input_tokens=1_000, max_output_tokens=1_000, tag=make_tag())

    actual = ledger.settle(rid, Usage(input_tokens=800, cached_input_tokens=0, output_tokens=200))

    assert actual == pytest.approx(800 * 0.10e-6 + 200 * 0.50e-6)
    assert ledger.spent_usd() == pytest.approx(actual)
    [row] = _rows(path)
    assert (row["state"], row["overrun"]) == ("settled", 0)


def test_settle_refuses_reservation_that_is_not_open(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite")
    rid = _reserve_eighth(ledger)
    ledger.release(rid)

    with pytest.raises(ValueError, match="is released, not open"):
        ledger.settle(rid, Usage(input_tokens=1))


def test_release_frees_the_reservation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite", phase_cap_usd=0.125)
    rid = _reserve_eighth(ledger)

    ledger.release(rid)

    assert ledger.spent_usd() == 0.0
    _reserve_eighth(ledger)


def test_forfeit_and_open_reservations_count_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    first = _ledger(path, phase_cap_usd=0.25)
    first.forfeit(_reserve_eighth(first))
    _reserve_eighth(first)  # left open, as if the process died mid-call

    restarted = _ledger(path, phase_cap_usd=0.25)

    assert restarted.spent_usd() == pytest.approx(0.25)
    assert restarted.spent_usd(phase="test") == pytest.approx(0.25)
    with pytest.raises(BudgetExceeded):
        _reserve_eighth(restarted)


def test_spent_usd_filters_by_phase(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    dev = CostLedger(path, phase="dev", phase_cap_usd=100.0, hard_cap_usd=200.0)
    dev.reserve(EIGHTH, input_tokens=0, max_output_tokens=1, tag=make_tag(phase="dev"))
    test = _ledger(path)
    _reserve_eighth(test)
    _reserve_eighth(test)

    assert (test.spent_usd(phase="dev"), test.spent_usd(phase="test"), test.spent_usd()) == (
        pytest.approx(0.125),
        pytest.approx(0.25),
        pytest.approx(0.375),
    )


def test_two_processes_share_hard_cap(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    _ledger(path, hard_cap_usd=3.0)  # create the schema once up front
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results: QueueType[int] = ctx.Queue()
    workers = [
        ctx.Process(target=_reserve_until_refused, args=(path, start, results)) for _ in range(2)
    ]
    for worker in workers:
        worker.start()

    start.set()
    granted = [results.get(timeout=120) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)

    # hard cap $3.00 minus the $2.00 margin leaves exactly eight $0.125 reservations in total.
    assert [worker.exitcode for worker in workers] == [0, 0]
    assert sum(granted) == 8
    assert _ledger(path, hard_cap_usd=3.0).spent_usd() == pytest.approx(1.0)


def test_threads_share_hard_cap(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = _ledger(path, phase_cap_usd=1.0)

    def attempt(_: int) -> bool:
        try:
            _reserve_eighth(ledger)
        except BudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(20)))

    assert sum(outcomes) == 8
    assert len(_rows(path)) == 8


def test_underestimate_raises_safety_factor_and_records_overrun(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = _ledger(path)
    rid = ledger.reserve(LUNA, input_tokens=10_000, max_output_tokens=100, tag=make_tag())

    ledger.settle(rid, Usage(input_tokens=15_000, cached_input_tokens=0, output_tokens=100))
    ledger.reserve(LUNA, input_tokens=10_000, max_output_tokens=100, tag=make_tag())

    settled, second = _rows(path)
    assert settled["overrun"] == 1
    # observed ratio 1.5 -> f = 1.5 * 1.05 = 1.575
    assert second["reserved_usd"] == pytest.approx((10_000 * 1.575 + 64) * 0.10e-6 + 100 * 0.50e-6)


def test_safety_factor_never_drops_below_initial_value(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    ledger = _ledger(path)
    rid = ledger.reserve(LUNA, input_tokens=10_000, max_output_tokens=0, tag=make_tag())

    ledger.settle(rid, Usage(input_tokens=5_000))
    ledger.reserve(LUNA, input_tokens=10_000, max_output_tokens=0, tag=make_tag())

    _, second = _rows(path)
    assert second["reserved_usd"] == pytest.approx((10_000 * 1.25 + 64) * 0.10e-6)


def test_cost_by_groups_by_tag(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite")
    for arm in ("A1", "A1", "A4"):
        rid = ledger.reserve(LUNA, input_tokens=10, max_output_tokens=10, tag=make_tag(arm=arm))
        ledger.settle(rid, Usage(input_tokens=1_000_000, cached_input_tokens=0, output_tokens=0))
    _reserve_eighth(ledger, arm="A4", component="consolidation")
    ledger.release(_reserve_eighth(ledger, arm="A4"))
    _reserve_eighth(ledger, run_id="other-run")

    rows = ledger.cost_by(phase="test", run_id="run-test")

    by_key = {(r["arm"], r["component"]): r for r in rows}
    assert set(by_key) == {("A1", "test"), ("A4", "test"), ("A4", "consolidation")}
    assert (by_key["A1", "test"]["calls"], by_key["A1", "test"]["usd"]) == (2, pytest.approx(0.2))
    assert (by_key["A4", "test"]["calls"], by_key["A4", "test"]["input_tokens"]) == (1, 1_000_000)
    assert by_key["A4", "consolidation"]["usd"] == pytest.approx(0.125)
    assert by_key["A1", "test"]["stream_id"] == "s-test"
    assert by_key["A1", "test"]["session"] == 1
