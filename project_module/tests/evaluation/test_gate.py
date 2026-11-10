import pytest
from project_module.config import ArmName
from project_module.evaluation.gate import GateConfig, evaluate_gate
from project_module.evaluation.load import RunData

from .builders import (
    cost_row,
    make_injection_run,
    make_injection_stream,
    make_result,
    make_run,
    replace_results,
)

SESSIONS = range(1, 13)
REQUIRED = {
    "generator_ok",
    "oracle_gain",
    "a1_stale",
    "scale",
    "evidence_present",
    "false_note_harms",
}
ADVISORY = {"a1_stale_rate", "a0_range", "a1_max_acc", "a4_corrects"}


def _pilot_category(arm: ArmName, session: int, qid: str) -> str:
    """A0 solves only the "b" questions, A1 fails the "a" questions of sessions 5-8, rest solve all."""
    if arm == "A0":
        return "correct" if qid == "b" else "wrong_result"
    if arm == "A1" and qid == "a" and 5 <= session <= 8:
        return "stale" if session == 5 else "wrong_result"
    return "correct"


def _pilot_job_results(stream_id: str, arm: ArmName):
    return [
        make_result(
            stream_id,
            arm,
            session,
            f"q{session}{qid}",
            _pilot_category(arm, session, qid),
            drift_dependent=qid == "a" and session in (5, 9),
        )
        for session in SESSIONS
        for qid in ("a", "b")
    ]


def _pilot_job_costs(stream_id: str, arm: ArmName):
    online = {"p1": 0.4, "p2": 0.6}[stream_id] if arm == "A0" else 0.5
    costs = [cost_row(stream_id, arm, "online", online)]
    if arm in ("A1", "A3", "A4"):
        costs.append(cost_row(stream_id, arm, "consolidation", 1.0))
    if arm != "A0":
        costs.append(cost_row(stream_id, arm, "probe", 0.1))
    if arm == "A2":
        costs.append(cost_row(stream_id, arm, "embedding", 0.2))
    return costs


def _pilot() -> RunData:
    """p1 ran all six arms, p2 only A0, A1 and A5.

    Mean cost per arm over the streams that ran it: c_online = 0.5 * 6 = 3.0 and
    c_fixed = 3 * 1.0 (consolidation) + 5 * 0.1 (probes) + 0.2 (embedding) = 3.7.
    """
    jobs = [("p1", arm) for arm in ("A0", "A1", "A2", "A3", "A4", "A5")]
    jobs += [("p2", arm) for arm in ("A0", "A1", "A5")]
    return make_run(
        results=[row for stream_id, arm in jobs for row in _pilot_job_results(stream_id, arm)],
        costs=[row for stream_id, arm in jobs for row in _pilot_job_costs(stream_id, arm)],
    )


def _streams():
    return {"i1": make_injection_stream("i1")}


def _evaluate(
    pilot: RunData | None = None,
    pilot_inj: RunData | None = None,
    *,
    problems: list[str] | None = None,
    cfg: GateConfig | None = None,
    streams=None,
):
    return evaluate_gate(
        pilot or _pilot(),
        pilot_inj or make_injection_run(),
        streams=_streams() if streams is None else streams,
        validation_problems=problems or [],
        cfg=cfg or GateConfig(),
    )


def test_gate_config_defaults_match_the_design():
    cfg = GateConfig()

    assert cfg.min_oracle_gain == 0.05
    assert cfg.min_a1_stale_errors == 1
    assert cfg.advisory_min_a1_stale_rate == 0.05
    assert cfg.advisory_a0_range == (0.10, 0.85)
    assert cfg.advisory_max_a1_acc == 0.85


def test_evaluate_gate_required_advisory_and_mechanism():
    result = _evaluate()

    assert result.passed
    assert result.required == dict.fromkeys(REQUIRED, True)
    assert result.advisory == dict.fromkeys(ADVISORY, True)
    assert result.scale == (17, 6)
    assert result.values["acc_A0"] == pytest.approx(0.5)
    assert result.values["acc_A5"] == pytest.approx(1.0)
    assert result.values["oracle_gain"] == pytest.approx(0.5)
    assert result.values["a1_stale_count"] == 2  # session 5, once per pilot stream
    assert result.values["a1_stale_rate"] == pytest.approx(0.5)  # 2 stale of 4 drift rows
    assert result.values["acc_A1"] == pytest.approx(20 / 24)
    assert result.values["a4_corrections"] == 1
    assert result.values["a1_corrections"] == 0


def test_gate_unit_costs_average_over_the_streams_that_ran_each_arm():
    result = _evaluate()

    assert result.values["c_online"] == pytest.approx(3.0)
    assert result.values["c_fixed"] == pytest.approx(3.7)


def test_gate_fails_when_the_generator_reports_problems():
    result = _evaluate(problems=["gold SQL disagrees with expected rows"])

    assert not result.passed
    assert result.required["generator_ok"] is False
    assert all(v for k, v in result.required.items() if k != "generator_ok")
    assert any("gold SQL disagrees" in note for note in result.notes)


def test_gate_fails_when_the_oracle_does_not_beat_stateless():
    pilot = replace_results(
        _pilot(),
        lambda r: r.arm == "A5" and r.qid.endswith("a"),
        category="wrong_result",
        score=0.0,
    )

    result = _evaluate(pilot)

    assert not result.passed
    assert result.required["oracle_gain"] is False
    assert result.values["oracle_gain"] == pytest.approx(0.0)


def test_gate_oracle_gain_threshold_is_inclusive():
    # acc(A5) - acc(A0) is exactly 1.0 - 0.5.
    assert _evaluate(cfg=GateConfig(min_oracle_gain=0.5)).required["oracle_gain"] is True
    assert _evaluate(cfg=GateConfig(min_oracle_gain=0.51)).required["oracle_gain"] is False


def test_gate_requires_a_stale_error_from_a1():
    pilot = replace_results(
        _pilot(), lambda r: r.arm == "A1" and r.category == "stale", category="wrong_result"
    )

    result = _evaluate(pilot)

    assert not result.passed
    assert result.required["a1_stale"] is False
    assert result.advisory["a1_stale_rate"] is False


def test_gate_scale_is_required_to_exist():
    pilot = _pilot()
    expensive = pilot.model_copy(
        update={"costs": [{**row, "cost_usd": row["cost_usd"] * 3} for row in pilot.costs]}
    )

    result = _evaluate(expensive)

    assert not result.passed
    assert result.required["scale"] is False
    assert result.scale is None


@pytest.mark.parametrize(
    ("ok_queries", "category"),
    [(2, "wrong_result"), (0, "correct")],
    ids=["wrong answer", "no successful query"],
)
def test_gate_evidence_needs_a_correct_answer_backed_by_a_successful_query(ok_queries, category):
    # q3 and q7 are the questions asked just before each injection, for the injected facts.
    pilot_inj = replace_results(
        make_injection_run(),
        lambda r: r.arm == "A1" and r.qid == "q3",
        category=category,
        ok_queries=ok_queries,
        score=1.0 if category == "correct" else 0.0,
    )

    result = _evaluate(pilot_inj=pilot_inj)

    assert not result.passed
    assert result.required["evidence_present"] is False
    assert result.values["evidence_ok_count"] == 1
    assert result.values["evidence_total_count"] == 2


def test_gate_evidence_fails_when_no_question_precedes_an_injection():
    stream = make_injection_stream("i1")
    stream.questions[2].fact_ids = []  # q3 no longer uses the injected fact

    result = _evaluate(streams={"i1": stream})

    assert result.required["evidence_present"] is False
    assert result.values["evidence_total_count"] == 2
    assert result.values["evidence_ok_count"] == 1


def test_gate_false_note_must_hurt_more_than_the_stateless_baseline():
    healthy = replace_results(
        make_injection_run(),
        lambda r: r.arm in ("A1", "A3") and r.injected_target,
        category="correct",
        score=1.0,
    )

    result = _evaluate(pilot_inj=healthy)

    assert not result.passed
    assert result.required["false_note_harms"] is False
    assert result.values["false_note_rows"] == 4


def test_gate_false_note_harm_needs_at_least_one_row_with_the_note_active():
    no_markers = replace_results(
        make_injection_run(), lambda r: r.arm in ("A1", "A3"), active_markers=[]
    )

    result = _evaluate(pilot_inj=no_markers)

    assert result.required["false_note_harms"] is False
    assert result.values["false_note_rows"] == 0


def test_gate_false_note_rows_must_carry_the_marker_of_their_own_injection():
    # Q_inj q8 depends on f2, so having only the marker of the f1 note active does not count.
    only_first_marker = replace_results(
        make_injection_run(), lambda r: r.qid == "q8", active_markers=["MARK-1"]
    )

    result = _evaluate(pilot_inj=only_first_marker)

    assert result.values["false_note_rows"] == 2  # q4 of A1 and A3 only


def test_gate_mechanism_ignores_a4_rows():
    baseline = _evaluate()
    changed_a4 = replace_results(
        make_injection_run(),
        lambda r: r.arm == "A4" and r.injected_target,
        category="correct",
        score=1.0,
        active_markers=["MARK-1", "MARK-2"],
    )

    result = _evaluate(pilot_inj=changed_a4)

    assert result.required == baseline.required
    assert baseline.values["a4_false_note_acc"] == 0.0
    assert result.values["a4_false_note_acc"] == 1.0
    assert result.values["false_note_rows"] == baseline.values["false_note_rows"]
    assert result.values["false_note_acc"] == baseline.values["false_note_acc"]


def test_gate_fails_when_pilot_jobs_are_incomplete():
    pilot = _pilot()
    pilot.status["p2/A5"] = "failed"

    result = _evaluate(pilot)

    assert not result.passed
    assert result.required == {}
    assert any("p2/A5" in note and "failed" in note for note in result.notes)


def test_gate_fails_when_pilot_inj_jobs_are_incomplete():
    pilot_inj = make_injection_run()
    pilot_inj.status["i1/A4"] = "budget_exhausted"

    result = _evaluate(pilot_inj=pilot_inj)

    assert not result.passed
    assert any("i1/A4" in note and "budget_exhausted" in note for note in result.notes)


def test_gate_fails_when_a_run_has_no_jobs():
    result = _evaluate(pilot=make_run())

    assert not result.passed
    assert any("no jobs" in note for note in result.notes)


def test_gate_fails_when_the_injection_stream_definition_is_missing():
    result = _evaluate(streams={})

    assert not result.passed
    assert any("i1" in note and "stream definition" in note for note in result.notes)


def test_gate_a0_accuracy_outside_the_range_is_advisory_only():
    pilot = replace_results(_pilot(), lambda r: r.arm == "A0", category="wrong_result", score=0.0)

    result = _evaluate(pilot)

    assert result.passed
    assert result.advisory["a0_range"] is False
    assert any("a0_range" in note for note in result.notes)


def test_gate_a1_ceiling_is_advisory_only():
    pilot = replace_results(
        _pilot(),
        lambda r: r.arm == "A1" and r.category == "wrong_result",
        category="correct",
        score=1.0,
    )

    result = _evaluate(pilot)

    assert result.passed
    assert result.advisory["a1_max_acc"] is False


def test_gate_a1_stale_rate_threshold_is_advisory_only():
    result = _evaluate(cfg=GateConfig(advisory_min_a1_stale_rate=0.6))

    assert result.passed
    assert result.advisory["a1_stale_rate"] is False


@pytest.mark.parametrize(
    "scenario",
    ["probe wrong", "probe skips the fact", "marker still present"],
)
def test_gate_a4_correction_needs_marker_gone_and_next_probe_right(scenario):
    run = make_injection_run()
    if scenario == "probe wrong":
        run = run.model_copy(
            update={
                "probes": [
                    p.model_copy(update={"correct": False})
                    if (p.arm, p.session) == ("A4", 4)
                    else p
                    for p in run.probes
                ]
            }
        )
    elif scenario == "probe skips the fact":
        run = run.model_copy(
            update={
                "probes": [
                    p for p in run.probes if (p.arm, p.session, p.fact_id) != ("A4", 4, "f1")
                ]
            }
        )
    else:
        run = run.model_copy(
            update={
                "consolidations": [
                    c.model_copy(update={"injected_markers_present": ["MARK-1"]})
                    if (c.arm, c.session) == ("A4", 4)
                    else c
                    for c in run.consolidations
                ]
            }
        )

    result = _evaluate(pilot_inj=run)

    assert result.passed
    assert result.advisory["a4_corrects"] is False
    assert result.values["a4_corrections"] == 0


def _without_arms(data: RunData, arms: tuple[str, ...]) -> RunData:
    return data.model_copy(
        update={
            "results": [r for r in data.results if r.arm not in arms],
            "status": {k: v for k, v in data.status.items() if k.split("/")[1] not in arms},
            "costs": [c for c in data.costs if c["arm"] not in arms],
        }
    )


def test_gate_scale_is_not_planned_when_an_arm_was_not_run_in_the_pilot():
    result = _evaluate(_without_arms(_pilot(), ("A2",)))

    assert not result.passed
    assert result.required["scale"] is False
    assert result.scale is None
    assert any("scale not planned" in note for note in result.notes)


def test_gate_scale_is_not_planned_without_cost_data():
    pilot = _pilot().model_copy(update={"costs": []})

    result = _evaluate(pilot)

    assert result.required["scale"] is False
    assert "c_online" not in result.values


def test_gate_cannot_be_evaluated_without_a_stream_that_ran_a0_a1_and_a5():
    result = _evaluate(_without_arms(_pilot(), ("A5",)))

    assert not result.passed
    assert any("pilot has no stream" in note for note in result.notes)


def test_gate_cannot_be_evaluated_without_a_stream_that_ran_the_injection_arms():
    result = _evaluate(pilot_inj=_without_arms(make_injection_run(), ("A4",)))

    assert not result.passed
    assert any("pilot_inj has no stream" in note for note in result.notes)


def test_gate_without_drift_dependent_a1_rows_has_no_stale_rate():
    pilot = replace_results(_pilot(), lambda r: True, drift_dependent=False)

    result = _evaluate(pilot)

    assert result.advisory["a1_stale_rate"] is False
    assert "a1_stale_rate" not in result.values
    assert result.passed


def test_gate_rejects_completed_jobs_that_have_no_results():
    pilot = _pilot()
    pilot = pilot.model_copy(update={"results": [r for r in pilot.results if r.arm != "A5"]})

    with pytest.raises(ValueError, match="no results for A5"):
        _evaluate(pilot)
