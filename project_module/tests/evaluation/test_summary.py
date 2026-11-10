import json

import pytest
from project_module.evaluation.summary import (
    mechanical_summary,
    note_outcomes,
    summarize_run,
)

from .builders import (
    cost_row,
    make_consolidation,
    make_injection_run,
    make_injection_stream,
    make_probe,
    make_result,
    make_run,
)


def _arm_results(stream_id, arm, correct, *, wrong="wrong_result", n=20, **flags):
    """`n` questions in sessions of 5; the first `correct` are answered right."""
    return [
        make_result(
            stream_id,
            arm,
            i // 5 + 1,
            f"q{i}",
            "correct" if i < correct else wrong,
            **flags,
        )
        for i in range(n)
    ]


def _run_with(counts, *, n=20, **kwargs):
    """`counts` maps an arm to the number of correct answers in each stream s1, s2, ..."""
    results = [
        row
        for arm, per_stream in counts.items()
        for index, correct in enumerate(per_stream, start=1)
        for row in _arm_results(f"s{index}", arm, correct, n=n)
    ]
    return make_run(results=results, **kwargs)


def test_summarize_run_reports_the_primary_comparison_with_its_interval():
    data = _run_with({"A4": [18, 16, 18, 17], "A1": [14, 14, 15, 16], "A0": [8, 8, 8, 8]})

    summary = summarize_run(data, arms=["A0", "A1", "A4"])

    primary = summary["primary"]
    assert (primary["treatment"], primary["control"]) == ("A4", "A1")
    # Per-stream differences: 0.20, 0.10, 0.15, 0.05
    assert primary["mean"] == pytest.approx(0.125)
    assert 0.05 <= primary["low"] <= primary["mean"] <= primary["high"] <= 0.20
    assert primary["n"] == 4
    assert primary["verdict"] == "supported"
    assert primary["practical_target_met"] is True


@pytest.mark.parametrize(
    ("a4", "a1", "verdict"),
    [
        ([16, 14, 16, 14], [14, 16, 14, 16], "not_shown"),
        ([10, 10, 10, 10], [14, 14, 14, 14], "reversed"),
        ([14, 14, 14, 14], [14, 14, 14, 14], "not_shown"),
    ],
    ids=["interval spans zero", "control clearly higher", "identical"],
)
def test_summarize_run_verdict_follows_the_interval_not_the_point_estimate(a4, a1, verdict):
    data = _run_with({"A4": a4, "A1": a1})

    summary = summarize_run(data, arms=["A1", "A4"])

    assert summary["primary"]["verdict"] == verdict


def test_summarize_run_practical_target_is_reported_separately_from_the_verdict():
    data = _run_with({"A4": [30, 30, 30, 30], "A1": [29, 29, 29, 29]}, n=50)

    primary = summarize_run(data, arms=["A1", "A4"])["primary"]

    assert primary["verdict"] == "supported"  # the interval is the point 0.02
    assert primary["practical_target_met"] is False


def test_summarize_run_lists_only_exploratory_pairs_whose_arms_were_run():
    data = _run_with({"A4": [18, 16], "A1": [14, 14], "A0": [8, 8]})

    summary = summarize_run(data, arms=["A0", "A1", "A4"])

    assert set(summary["exploratory"]) == {"A4-A0", "A1-A0"}
    assert summary["exploratory"]["A4-A0"]["mean"] == pytest.approx(0.45)


def test_summarize_run_lists_every_preregistered_exploratory_pair_for_six_arms():
    counts = {arm: [10, 12] for arm in ("A0", "A1", "A2", "A3", "A4", "A5")}

    summary = summarize_run(_run_with(counts), arms=list(counts))

    assert set(summary["exploratory"]) == {"A4-A3", "A4-A2", "A4-A0", "A1-A0", "A5-A0"}


def test_summarize_run_drops_streams_where_any_arm_did_not_complete():
    data = _run_with({"A4": [18, 16, 18], "A1": [14, 14, 15]})
    data.status["s3/A1"] = "failed"

    summary = summarize_run(data, arms=["A1", "A4"])

    assert summary["streams"] == {"analyzed": ["s1", "s2"], "excluded": {"s3": {"A1": "failed"}}}
    assert summary["primary"]["n"] == 2


def test_summarize_run_reports_accuracy_and_stratified_accuracy_per_arm():
    results = [
        make_result("s1", "A1", 1, "q1", "correct", injected_target=True),
        make_result("s1", "A1", 1, "q2", "wrong_result", injected_target=True),
        make_result("s1", "A1", 1, "q3", "correct"),
        make_result("s1", "A1", 1, "q4", "correct"),
    ]

    arm = summarize_run(make_run(results=results), arms=["A1"])["by_arm"]["A1"]

    assert arm["accuracy"] == 0.75
    assert arm["accuracy_q_inj"] == 0.5
    assert arm["accuracy_q_natural"] == 1.0


def test_summarize_run_stratum_without_rows_is_none():
    results = [make_result("s1", "A0", 1, "q1", "correct")]

    arm = summarize_run(make_run(results=results), arms=["A0"])["by_arm"]["A0"]

    assert arm["accuracy_q_inj"] is None
    assert arm["accuracy_q_natural"] == 1.0


def test_summarize_run_reports_official_score_apart_from_accuracy():
    results = [
        make_result("s1", "A0", 1, "q1", "correct", score=0.6),
        make_result("s1", "A0", 1, "q2", "wrong_result"),
    ]

    arm = summarize_run(make_run(results=results), arms=["A0"])["by_arm"]["A0"]

    assert arm["accuracy"] == 0.5
    assert arm["score"] == pytest.approx(0.3)


def test_summarize_run_classifies_errors_and_stale_rate():
    results = [
        make_result("s1", "A1", 5, "q1", "stale", drift_dependent=True),
        make_result("s1", "A1", 5, "q2", "correct", drift_dependent=True),
        make_result("s1", "A1", 5, "q3", "sql_error"),
        make_result("s1", "A1", 5, "q4", "no_submission"),
    ]

    arm = summarize_run(make_run(results=results), arms=["A1"])["by_arm"]["A1"]

    assert arm["error_breakdown"] == {
        "correct": 1,
        "stale": 1,
        "sql_error": 1,
        "wrong_result": 0,
        "no_submission": 1,
    }
    assert arm["stale_rate"] == 0.25
    assert arm["stale_rate_drift_dependent"] == 0.5


def test_summarize_run_reports_probe_accuracy_with_the_injected_subset():
    results = [make_result("s1", "A1", 1, "q1"), make_result("s1", "A0", 1, "q1")]
    probes = [
        make_probe("s1", "A1", 4, "f1", correct=True, injected=False),
        make_probe("s1", "A1", 4, "f2", correct=False, injected=False),
        make_probe("s1", "A1", 4, "f3", correct=False, injected=True),
    ]

    by_arm = summarize_run(make_run(results=results, probes=probes), arms=["A0", "A1"])["by_arm"]

    assert by_arm["A1"]["probe_accuracy"] == pytest.approx(1 / 3)
    assert by_arm["A1"]["probe_accuracy_injected"] == 0.0
    assert by_arm["A0"]["probe_accuracy"] is None


def test_summarize_run_reports_session_accuracy_per_arm():
    results = [
        make_result("s1", "A0", 1, "q1", "correct"),
        make_result("s1", "A0", 2, "q1", "wrong_result"),
    ]

    arm = summarize_run(make_run(results=results), arms=["A0"])["by_arm"]["A0"]

    assert arm["session_accuracy"] == {"1": 1.0, "2": 0.0}


def test_summarize_run_reports_cost_per_stream_component_and_correct_answer():
    results = [
        *_arm_results("s1", "A1", 4, n=5),
        *_arm_results("s2", "A1", 2, n=5),
    ]
    costs = [
        cost_row("s1", "A1", "online", 1.0),
        cost_row("s1", "A1", "consolidation", 2.0),
        cost_row("s2", "A1", "online", 1.0),
        cost_row("s2", "A1", "consolidation", 1.0),
    ]

    arm = summarize_run(make_run(results=results, costs=costs), arms=["A1"])["by_arm"]["A1"]

    assert arm["cost_per_stream_usd"] == pytest.approx(2.5)
    assert arm["cost_by_component_usd"] == {"online": pytest.approx(1.0), "consolidation": 1.5}
    assert arm["cost_per_correct_usd"] == pytest.approx(5.0 / 6)  # 6 correct answers cost $5


def test_summarize_run_cost_per_correct_is_none_without_correct_answers():
    results = _arm_results("s1", "A0", 0, n=3)

    arm = summarize_run(
        make_run(results=results, costs=[cost_row("s1", "A0", "online", 1.0)]), arms=["A0"]
    )["by_arm"]["A0"]

    assert arm["cost_per_correct_usd"] is None


def test_summarize_run_reports_run_cost_including_excluded_streams():
    data = _run_with(
        {"A1": [4, 4]},
        n=5,
        costs=[cost_row("s1", "A1", "online", 1.0), cost_row("s2", "A1", "online", 2.0)],
    )
    data.status["s2/A1"] = "failed"

    cost = summarize_run(data, arms=["A1"])["cost"]

    assert cost == {"run_total_usd": pytest.approx(3.0), "by_component_usd": {"online": 3.0}}


def test_summarize_run_reports_false_note_survival_and_correction_when_streams_are_given():
    data = make_injection_run()
    streams = {"i1": make_injection_stream("i1")}

    by_arm = summarize_run(data, arms=["A0", "A1", "A3", "A4"], streams=streams)["by_arm"]

    assert by_arm["A0"]["false_note"] is None
    assert by_arm["A1"]["false_note"] == {
        "injections": 2,
        "corrections": 0,
        "correction_rate": 0.0,
        "survival_rate": 1.0,
    }
    assert by_arm["A4"]["false_note"]["corrections"] == 1
    assert by_arm["A4"]["false_note"]["correction_rate"] == 0.5
    # First note: gone after each of sessions 3-11 (9 checks). Second note: kept in 7-11 (5).
    assert by_arm["A4"]["false_note"]["survival_rate"] == pytest.approx(5 / 14)


def test_summarize_run_leaves_false_note_blocks_empty_without_stream_definitions():
    by_arm = summarize_run(make_injection_run(), arms=["A1", "A4"])["by_arm"]

    assert by_arm["A4"]["false_note"] is None


def test_summarize_run_without_analyzable_streams_has_no_comparisons():
    summary = summarize_run(make_run(), arms=["A1", "A4"])

    assert summary["streams"] == {"analyzed": [], "excluded": {}}
    assert summary["primary"] is None
    assert summary["exploratory"] == {}


def test_summarize_run_output_is_json_serialisable():
    data = make_injection_run()
    summary = summarize_run(
        data, arms=["A0", "A1", "A3", "A4"], streams={"i1": make_injection_stream("i1")}
    )

    assert json.loads(json.dumps(summary)) == summary


def test_note_outcomes_follow_each_injection_through_consolidations_and_probes():
    data = make_injection_run()

    outcomes = note_outcomes(data, "A4", {"i1": make_injection_stream("i1")}, ["i1"])

    assert [(o.marker, o.corrected) for o in outcomes] == [("MARK-1", True), ("MARK-2", False)]
    assert outcomes[0].present_after == [False] * 9
    assert outcomes[1].present_after == [True] * 5


def test_note_outcomes_not_corrected_without_a_probe_after_the_injection():
    data = make_injection_run()
    data = data.model_copy(update={"probes": []})

    outcomes = note_outcomes(data, "A4", {"i1": make_injection_stream("i1")}, ["i1"])

    assert [o.corrected for o in outcomes] == [False, False]


def test_mechanical_summary_has_no_accuracy_by_arm():
    data = make_injection_run()

    summary = mechanical_summary(data)

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield str(key)
                yield from keys(value)

    forbidden = ("accuracy", "score", "correct", "stale", "wrong", "sql_error")
    assert not [k for k in keys(summary) if any(word in k for word in forbidden)]


def test_mechanical_summary_reports_tool_success_no_submission_lint_and_cost():
    results = [
        make_result("s1", "A1", 1, "q1", "correct", tool_calls=4, tool_errors=1),
        make_result("s1", "A1", 1, "q2", "no_submission", tool_calls=6, tool_errors=1),
        make_result("s1", "A4", 1, "q1", "wrong_result", tool_calls=10, tool_errors=0),
        make_result("s1", "A4", 1, "q2", "correct", tool_calls=0, tool_errors=0),
    ]
    consolidations = [
        make_consolidation("s1", "A1", 1, lint_problems=["broken link", "missing index entry"]),
        make_consolidation("s1", "A4", 1),
        make_consolidation("s1", "A4", 2, lint_problems=["stub in index"]),
    ]
    costs = [cost_row("s1", "A1", "online", 0.5), cost_row("s1", "A4", "consolidation", 1.5)]

    summary = mechanical_summary(
        make_run(results=results, consolidations=consolidations, costs=costs)
    )

    assert summary["n_results"] == 4
    assert summary["tool_call_success_rate"] == pytest.approx(18 / 20)
    assert summary["no_submission_rate"] == 0.25
    assert summary["consolidation"]["runs"] == 3
    assert summary["consolidation"]["lint_problems"] == 3
    assert summary["consolidation"]["runs_with_lint_problems"] == 2
    assert summary["consolidation"]["stopped"] == {"final": 3}
    assert summary["cost_usd"] == {
        "total": 2.0,
        "by_component": {"online": 0.5, "consolidation": 1.5},
    }
    assert summary["job_status"] == {"completed": 2}
    assert summary["by_arm"]["A1"]["tool_call_success_rate"] == pytest.approx(0.8)
    assert summary["by_arm"]["A1"]["no_submission_rate"] == 0.5
    assert summary["by_arm"]["A4"]["lint_problems"] == 1
    assert summary["by_arm"]["A4"]["cost_usd"] == 1.5


def test_mechanical_summary_without_tool_calls_has_no_success_rate():
    results = [make_result("s1", "A0", 1, "q1", tool_calls=0, tool_errors=0)]

    summary = mechanical_summary(make_run(results=results))

    assert summary["tool_call_success_rate"] is None


def test_mechanical_summary_reports_stop_reasons_and_injected_updates_per_arm():
    consolidations = [
        make_consolidation(
            "s1", "A1", 1, stopped="summarizer_review:final;summarizer_rewrite:final"
        ),
        make_consolidation(
            "s1",
            "A1",
            2,
            stopped="summarizer_review:max_turns;summarizer_rewrite:final",
            injected_updated=False,
        ),
        make_consolidation("s1", "A4", 1, stopped="analyzer:final;organizer:max_turns"),
    ]

    summary = mechanical_summary(make_run(consolidations=consolidations))

    assert summary["consolidation"]["injected_updated_rate"] == pytest.approx(2 / 3)
    assert summary["by_arm"]["A1"]["consolidation_stopped"] == {
        "summarizer_review:final;summarizer_rewrite:final": 1,
        "summarizer_review:max_turns;summarizer_rewrite:final": 1,
    }
    assert summary["by_arm"]["A1"]["injected_updated_rate"] == 0.5
    assert summary["by_arm"]["A4"]["consolidation_stopped"] == {
        "analyzer:final;organizer:max_turns": 1
    }
    assert summary["by_arm"]["A4"]["injected_updated_rate"] == 1.0


def test_mechanical_summary_has_no_injected_rate_without_consolidations():
    summary = mechanical_summary(make_run(results=[make_result("s1", "A0", 1, "q1")]))

    assert summary["consolidation"]["injected_updated_rate"] is None
    assert summary["by_arm"]["A0"]["injected_updated_rate"] is None
    assert summary["by_arm"]["A0"]["consolidation_stopped"] == {}
