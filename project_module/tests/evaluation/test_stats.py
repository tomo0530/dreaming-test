import pytest
from project_module.evaluation.stats import (
    compare_arms,
    cost_per_stream_arm,
    paired_bootstrap,
    per_stream_accuracy,
    per_stream_score,
    plan_main_scale,
    session_accuracy,
)

from .builders import cost_row, make_result, make_run


def test_paired_bootstrap_ci_contains_mean_and_is_reproducible():
    diffs = [0.05, 0.10, -0.02, 0.08, 0.03, 0.12, 0.00, 0.07]

    first = paired_bootstrap(diffs)
    second = paired_bootstrap(diffs)

    assert first == second
    assert first.mean == pytest.approx(sum(diffs) / len(diffs))
    assert first.low <= first.mean <= first.high
    assert first.n == len(diffs)


def test_paired_bootstrap_zero_variance_gives_point_interval():
    ci = paired_bootstrap([0.1, 0.1, 0.1, 0.1])

    assert ci.low == ci.mean == ci.high == 0.1


def test_paired_bootstrap_single_difference_is_a_point_interval():
    ci = paired_bootstrap([0.25])

    assert (ci.low, ci.mean, ci.high, ci.n) == (0.25, 0.25, 0.25, 1)


def test_paired_bootstrap_two_values_spans_both_values():
    # Resample means of [0, 1] are 0, 0.5 or 1 with probabilities 1/4, 1/2, 1/4, so the
    # 2.5th and 97.5th percentiles are the extremes.
    ci = paired_bootstrap([0.0, 1.0])

    assert (ci.low, ci.mean, ci.high) == (0.0, 0.5, 1.0)


def test_paired_bootstrap_interval_shifts_with_a_constant():
    diffs = [0.05, 0.10, -0.02, 0.08, 0.03, 0.12]

    base = paired_bootstrap(diffs)
    shifted = paired_bootstrap([d + 1.0 for d in diffs])

    assert shifted.low == pytest.approx(base.low + 1.0)
    assert shifted.high == pytest.approx(base.high + 1.0)


def test_paired_bootstrap_interval_excludes_zero_for_consistently_positive_diffs():
    ci = paired_bootstrap([0.04, 0.06, 0.05, 0.07, 0.03, 0.05, 0.06, 0.04])

    assert ci.low > 0


def test_paired_bootstrap_seed_changes_the_resamples():
    diffs = [0.05, 0.10, -0.02, 0.08, 0.03, 0.12, 0.00, 0.07]

    default = paired_bootstrap(diffs, n_resamples=200)
    other = paired_bootstrap(diffs, n_resamples=200, seed=1)

    assert (default.low, default.high) != (other.low, other.high)


def test_paired_bootstrap_rejects_empty_input():
    with pytest.raises(ValueError, match="at least one difference"):
        paired_bootstrap([])


def test_paired_bootstrap_rejects_non_finite_values():
    with pytest.raises(ValueError, match="finite"):
        paired_bootstrap([0.1, float("nan")])


def test_paired_bootstrap_rejects_non_positive_resample_count():
    with pytest.raises(ValueError, match="n_resamples"):
        paired_bootstrap([0.1, 0.2], n_resamples=0)


def _two_stream_run():
    return make_run(
        results=[
            make_result("s1", "A4", 1, "q1", "correct"),
            make_result("s1", "A4", 1, "q2", "correct"),
            make_result("s1", "A1", 1, "q1", "correct"),
            make_result("s1", "A1", 1, "q2", "stale"),
            make_result("s2", "A4", 1, "q1", "correct"),
            make_result("s2", "A4", 1, "q2", "wrong_result"),
            make_result("s2", "A1", 1, "q1", "sql_error"),
            make_result("s2", "A1", 1, "q2", "wrong_result"),
        ]
    )


def test_per_stream_score_averages_within_each_stream():
    data = _two_stream_run()

    scores = per_stream_score(data, "A4", ["s1", "s2"])

    assert scores == {"s1": 1.0, "s2": 0.5}


def test_per_stream_score_applies_where_and_omits_empty_streams():
    data = make_run(
        results=[
            make_result("s1", "A4", 1, "q1", "correct", injected_target=True),
            make_result("s1", "A4", 1, "q2", "wrong_result"),
            make_result("s2", "A4", 1, "q1", "correct"),
        ]
    )

    scores = per_stream_score(data, "A4", ["s1", "s2"], where=lambda r: r.injected_target)

    assert scores == {"s1": 1.0}


def test_per_stream_score_ignores_streams_outside_the_request():
    data = _two_stream_run()

    assert per_stream_score(data, "A4", ["s2"]) == {"s2": 0.5}


def test_per_stream_accuracy_counts_correct_answers_not_score():
    data = make_run(
        results=[
            make_result("s1", "A0", 1, "q1", "correct", score=0.8),
            make_result("s1", "A0", 1, "q2", "wrong_result"),
        ]
    )

    assert per_stream_score(data, "A0", ["s1"]) == {"s1": pytest.approx(0.4)}
    assert per_stream_accuracy(data, "A0", ["s1"]) == {"s1": 0.5}


def test_compare_arms_bootstraps_the_per_stream_differences():
    data = _two_stream_run()

    ci = compare_arms(data, "A4", "A1", streams=["s1", "s2"])

    # A4 - A1: s1 = 1.0 - 0.5, s2 = 0.5 - 0.0
    assert ci.mean == pytest.approx(0.5)
    assert ci.n == 2
    assert ci == paired_bootstrap([0.5, 0.5])


def test_compare_arms_pairs_only_streams_present_in_both_arms():
    data = make_run(
        results=[
            make_result("s1", "A4", 1, "q1", "correct", injected_target=True),
            make_result("s1", "A1", 1, "q1", "wrong_result", injected_target=True),
            make_result("s2", "A4", 1, "q1", "correct"),
            make_result("s2", "A1", 1, "q1", "wrong_result"),
        ]
    )

    ci = compare_arms(data, "A4", "A1", streams=["s1", "s2"], where=lambda r: r.injected_target)

    assert ci.n == 1
    assert ci.mean == 1.0


def test_compare_arms_rejects_streams_without_data():
    data = _two_stream_run()

    with pytest.raises(ValueError, match="no stream"):
        compare_arms(data, "A4", "A1", streams=["missing"])


def test_cost_per_stream_arm_sums_components_per_job():
    data = make_run(
        costs=[
            cost_row("s1", "A4", "online", 0.25, session=1),
            cost_row("s1", "A4", "online", 0.25, session=2),
            cost_row("s1", "A4", "consolidation", 1.0, session=2),
            cost_row("s2", "A4", "online", 0.5),
        ]
    )

    assert cost_per_stream_arm(data) == {
        "s1/A4": {"online": 0.5, "consolidation": 1.0},
        "s2/A4": {"online": 0.5},
    }


def test_session_accuracy_averages_streams_with_equal_weight():
    data = make_run(
        results=[
            # s1 session 1: 1 of 2 correct; s2 session 1: 1 of 1 correct.
            make_result("s1", "A0", 1, "q1", "correct"),
            make_result("s1", "A0", 1, "q2", "wrong_result"),
            make_result("s2", "A0", 1, "q1", "correct"),
            make_result("s1", "A0", 2, "q1", "wrong_result"),
            make_result("s2", "A0", 2, "q1", "wrong_result"),
        ]
    )

    assert session_accuracy(data, "A0", ["s1", "s2"]) == {1: 0.75, 2: 0.0}


def test_plan_main_scale_examples():
    assert plan_main_scale(4.0, 2.0) == (20, 6)  # c6 = 6.0 -> 20
    assert plan_main_scale(5.0, 2.0) == (17, 6)  # c6 = 7.0 -> 17
    assert plan_main_scale(6.0, 2.0) == (17, 5)  # c6 = 8.0 -> 15 < 16, c5 = 7.0 -> 17
    # c6 = 9.0 -> 13, c5 = 7.8 -> 15 < 16, but 16 * 7.8 = 124.8 <= 130
    assert plan_main_scale(7.2, 1.8) == (16, 5)
    assert plan_main_scale(7.2, 2.4) is None  # c5 = 8.4, 16 * 8.4 = 134.4 > 130


def test_plan_main_scale_caps_the_stream_count():
    assert plan_main_scale(0.5, 0.5) == (20, 6)


def test_plan_main_scale_honours_custom_caps():
    # c6 = 6.0 -> 10 streams, c5 = 5.17 -> 11 streams (both < 16), stretch: 16 * 5.17 = 82.7 <= 90
    assert plan_main_scale(5.0, 1.0, main_cap_usd=60.0, stretch_cap_usd=90.0) == (16, 5)


@pytest.mark.parametrize("c_online, c_fixed", [(0.0, 0.0), (-1.0, 2.0), (1.0, -0.5)])
def test_plan_main_scale_rejects_non_positive_unit_costs(c_online, c_fixed):
    with pytest.raises(ValueError, match="unit cost"):
        plan_main_scale(c_online, c_fixed)
