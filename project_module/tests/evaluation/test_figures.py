import pytest
from project_module.evaluation.figures import plot_pareto, plot_session_accuracy
from project_module.evaluation.summary import summarize_run

from .builders import cost_row, make_result, make_run

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _run():
    results = [
        make_result(stream_id, arm, session, f"q{session}", category)
        for stream_id in ("s1", "s2")
        for arm, accuracy_pattern in (("A0", "wwcw"), ("A1", "wccc"), ("A4", "ccwc"))
        for session, category in zip(
            (3, 4, 5, 6),
            ("correct" if c == "c" else "wrong_result" for c in accuracy_pattern),
            strict=True,
        )
    ]
    costs = [
        cost_row(stream_id, arm, "online", usd)
        for stream_id in ("s1", "s2")
        for arm, usd in (("A0", 0.5), ("A1", 1.5), ("A4", 2.0))
    ]
    return make_run(results=results, costs=costs)


def test_figures_write_png(tmp_path):
    data = _run()
    summary = summarize_run(data, arms=["A0", "A1", "A4"])

    plot_session_accuracy(data, tmp_path / "sessions.png")
    plot_pareto(summary, tmp_path / "pareto.png")

    for name in ("sessions.png", "pareto.png"):
        content = (tmp_path / name).read_bytes()
        assert content.startswith(PNG_SIGNATURE)
        assert len(content) > 5_000


def test_plot_functions_create_missing_output_directories(tmp_path):
    data = _run()

    plot_session_accuracy(data, tmp_path / "figures" / "nested" / "sessions.png")

    assert (tmp_path / "figures" / "nested" / "sessions.png").is_file()


def test_plot_session_accuracy_accepts_an_arm_subset_and_custom_migrations(tmp_path):
    plot_session_accuracy(_run(), tmp_path / "subset.png", arms=["A0", "A4"], migrations=(4,))

    assert (tmp_path / "subset.png").read_bytes().startswith(PNG_SIGNATURE)


def test_plot_session_accuracy_needs_a_stream_where_every_plotted_arm_completed(tmp_path):
    data = _run()
    data.status["s1/A1"] = "failed"
    data.status["s2/A1"] = "failed"

    with pytest.raises(ValueError, match="no stream"):
        plot_session_accuracy(data, tmp_path / "none.png")


def test_plot_pareto_rejects_a_summary_without_costed_arms(tmp_path):
    summary = summarize_run(make_run(results=[make_result("s1", "A0", 1, "q1")]), arms=["A0"])

    with pytest.raises(ValueError, match="cost"):
        plot_pareto(summary, tmp_path / "pareto.png")
