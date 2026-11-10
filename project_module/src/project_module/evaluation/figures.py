"""Figures for the report: accuracy by session and the accuracy-versus-cost Pareto plot."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from project_module.evaluation.load import RunData, analyzable_streams
from project_module.evaluation.stats import session_accuracy

_DPI = 150
_GRID_COLOR = "#d9d9d6"
_TEXT_COLOR = "#2b2b2a"
_SECONDARY_TEXT_COLOR = "#52514e"
_MIGRATION_COLOR = "#8a8984"

# One fixed colour, marker and label per arm, so an arm looks the same in every figure. Colours
# follow the reference categorical order (blue, orange, aqua, yellow, magenta, green); the
# markers and the labels carry identity as well, so colour is never the only cue.
_ARM_STYLE: dict[str, tuple[str, str, str]] = {
    "A0": ("#2a78d6", "o", "A0 Stateless"),
    "A1": ("#eb6834", "s", "A1 Rolling Summary"),
    "A2": ("#1baf7a", "^", "A2 Raw-RAG"),
    "A3": ("#eda100", "D", "A3 Organize-only"),
    "A4": ("#e87ba4", "v", "A4 Verified Dreaming"),
    "A5": ("#008300", "P", "A5 Oracle"),
}


def plot_session_accuracy(
    data: RunData,
    out: Path,
    *,
    migrations: tuple[int, ...] = (4, 8),
    arms: Sequence[str] | None = None,
) -> None:
    """Draw accuracy per session for each arm, with a vertical line at every migration.

    `arms` defaults to every arm that has results. Only streams in which all plotted arms
    completed are used, so every line is drawn from the same streams. A line at `m` marks the
    migration that happens after session `m`.
    """
    plotted = sorted({r.arm for r in data.results}) if arms is None else list(arms)
    streams = analyzable_streams(data, plotted)
    if not streams:
        raise ValueError(f"no stream has completed jobs for all of {plotted}")

    fig = Figure(figsize=(8.0, 4.5))
    ax = fig.subplots()
    for arm in plotted:
        color, marker, label = _ARM_STYLE[arm]
        by_session = session_accuracy(data, arm, streams)
        ax.plot(
            list(by_session),
            list(by_session.values()),
            color=color,
            marker=marker,
            markersize=6,
            linewidth=2,
            label=label,
        )
    for migration in migrations:
        ax.axvline(migration + 0.5, color=_MIGRATION_COLOR, linestyle="--", linewidth=1)
        ax.annotate(
            "migration",
            (migration + 0.5, 1.0),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
            color=_SECONDARY_TEXT_COLOR,
        )
    _style_axes(ax, xlabel="Session", ylabel="Accuracy")
    ax.set_ylim(0.0, 1.0)
    ax.set_title(
        f"Accuracy by session ({len(streams)} streams)", color=_TEXT_COLOR, fontsize=11, pad=22
    )
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), frameon=False, fontsize=9)
    _save(fig, out)


def plot_pareto(summary: dict[str, Any], out: Path) -> None:
    """Draw accuracy against mean cost per stream for each arm from a `summarize_run` dict.

    Error bars are the stream-bootstrap 95% intervals of the accuracy; the dashed line joins the
    arms that no cheaper arm beats on accuracy.
    """
    points = {
        arm: (stats["cost_per_stream_usd"], stats["accuracy"], stats["accuracy_ci"])
        for arm, stats in summary["by_arm"].items()
        if stats["cost_per_stream_usd"] is not None and stats["accuracy"] is not None
    }
    if not points:
        raise ValueError("summary has no arm with both accuracy and cost to plot")

    fig = Figure(figsize=(7.0, 4.5))
    ax = fig.subplots()
    for arm, (cost, accuracy, ci) in points.items():
        color, marker, label = _ARM_STYLE[arm]
        ax.errorbar(
            cost,
            accuracy,
            yerr=[[accuracy - ci["low"]], [ci["high"] - accuracy]],
            color=color,
            marker=marker,
            markersize=8,
            linewidth=2,
            capsize=3,
            linestyle="none",
        )
        ax.annotate(
            label,
            (cost, accuracy),
            xytext=(8, 6),
            textcoords="offset points",
            fontsize=9,
            color=_TEXT_COLOR,
        )
    frontier = _frontier([(cost, accuracy) for cost, accuracy, _ in points.values()])
    ax.plot(
        [c for c, _ in frontier],
        [a for _, a in frontier],
        color=_MIGRATION_COLOR,
        linestyle="--",
        linewidth=1,
        zorder=0,
    )
    _style_axes(ax, xlabel="Cost per stream (USD)", ylabel="Accuracy")
    ax.set_ylim(0.0, 1.0)
    ax.set_xlim(left=0.0, right=max(cost for cost, _, _ in points.values()) * 1.3)
    ax.set_title("Accuracy versus cost", color=_TEXT_COLOR, fontsize=11)
    _save(fig, out)


def _frontier(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Points not beaten on accuracy by any cheaper point, in increasing cost order."""
    frontier: list[tuple[float, float]] = []
    for cost, accuracy in sorted(points):
        if not frontier or accuracy > frontier[-1][1]:
            frontier.append((cost, accuracy))
    return frontier


def _style_axes(ax: Axes, *, xlabel: str, ylabel: str) -> None:
    ax.set_xlabel(xlabel, color=_SECONDARY_TEXT_COLOR)
    ax.set_ylabel(ylabel, color=_SECONDARY_TEXT_COLOR)
    ax.grid(True, color=_GRID_COLOR, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=_SECONDARY_TEXT_COLOR)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_GRID_COLOR)


def _save(fig: Figure, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    FigureCanvasAgg(fig)
    fig.savefig(out, format="png", dpi=_DPI, bbox_inches="tight", facecolor="white")
