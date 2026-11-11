from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fakes import FakeTransport, text_reply
from project_module.config import (
    SCHEMADRIFT_SWITCHES,
    ConsolidationBudget,
    ExperimentConfig,
    ModelSpec,
    load_experiment,
    load_models,
)
from project_module.layout import RunLayout, ledger_path
from project_module.manifest import config_sha256
from project_module.schemadrift.models import Fact, Stream
from project_module.tokens import count_tokens, truncate_to_tokens
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "configs" / "experiments"
FOLLOWUP = EXPERIMENTS / "followup"


def _write_config(tmp_path: Path, **overrides: object) -> Path:
    cfg: dict[str, object] = {
        "name": "t",
        "phase": "dev",
        "arms": ["A0", "A1"],
        "stream_ids": ["s1", "s2"],
        "seeds": [1, 2],
        "inject_false_notes": False,
        "agent_model": "gpt-6-luna",
        "consolidator_model": "gpt-6-luna",
        "embedding_model": "text-embedding-3-large",
        "budgets": {"phase_cap_usd": 1.0},
    }
    cfg.update(overrides)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_load_experiment_rejects_mismatched_seeds(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_experiment(_write_config(tmp_path, seeds=[1]))


def test_arms_for_applies_override(tmp_path: Path) -> None:
    cfg = load_experiment(_write_config(tmp_path, arm_overrides={"s1": ["A0", "A1", "A4"]}))
    assert cfg.arms_for("s1") == ["A0", "A1", "A4"]
    assert cfg.arms_for("s2") == ["A0", "A1"]


def test_phase_cap_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_experiment(_write_config(tmp_path, budgets={}))


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf", 0, -1])
@pytest.mark.parametrize("key", ["phase_cap_usd", "hard_cap_usd"])
def test_budget_caps_must_be_finite_and_positive(tmp_path: Path, key: str, value: object) -> None:
    budgets = {"phase_cap_usd": 1.0, key: value}
    path = tmp_path / "cfg.yaml"
    raw = yaml.safe_load(_write_config(tmp_path).read_text())
    raw["budgets"] = budgets
    # YAML's own spelling of NaN and infinity, as an experiment file would contain it.
    path.write_text(yaml.safe_dump(raw).replace(f"'{value}'", str(value)))
    with pytest.raises(ValidationError):
        load_experiment(path)


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -0.01])
@pytest.mark.parametrize("field", ["input_usd_per_m", "cached_input_usd_per_m", "output_usd_per_m"])
def test_model_prices_must_be_finite_and_non_negative(field: str, price: float) -> None:
    spec = {
        "deployment": "d",
        "input_usd_per_m": 1.0,
        "cached_input_usd_per_m": 0.1,
        "output_usd_per_m": 2.0,
        "kind": "chat",
        field: price,
    }
    with pytest.raises(ValidationError):
        ModelSpec.model_validate(spec)


def test_consolidator_output_limit_defaults_to_8192() -> None:
    assert ConsolidationBudget().max_completion_tokens == 8192


@pytest.mark.parametrize("path", sorted(EXPERIMENTS.glob("**/*.yaml")), ids=lambda p: p.stem)
def test_every_experiment_gives_consolidators_8192_output_tokens(path: Path) -> None:
    assert load_experiment(path).budgets.consolidation.max_completion_tokens == 8192


def test_load_models_reads_prices() -> None:
    models = load_models(REPO / "configs" / "models.yaml")
    luna = models["gpt-6-luna"]
    assert (luna.input_usd_per_m, luna.cached_input_usd_per_m, luna.output_usd_per_m) == (
        0.10,
        0.01,
        0.50,
    )
    assert models["text-embedding-3-large"].input_usd_per_m == 0.13


def test_stream_epoch_and_valid_facts(tmp_path: Path) -> None:
    facts = [
        Fact(
            fact_id="F2",
            kind="unit",
            subject="a",
            value="cents",
            valid_from_session=1,
            valid_until_session=4,
            leaf=False,
        ),
        Fact(
            fact_id="F1",
            kind="unit",
            subject="a",
            value="dollars",
            valid_from_session=5,
            valid_until_session=None,
            leaf=False,
        ),
    ]
    stream = Stream(
        stream_id="s",
        seed=1,
        root=tmp_path,
        sessions=12,
        questions_per_session=6,
        questions=[],
        facts=facts,
        injections=[],
    )
    assert [stream.epoch_of(s) for s in (1, 4, 5, 8, 9, 12)] == [1, 1, 2, 2, 3, 3]
    assert [f.fact_id for f in stream.valid_facts(4)] == ["F2"]
    assert [f.fact_id for f in stream.valid_facts(5)] == ["F1"]
    assert stream.db_path(2) == tmp_path / "epoch_2.sqlite"


def test_truncate_to_tokens_marks_truncation_within_limit() -> None:
    text = "word " * 500
    out = truncate_to_tokens(text, 50)
    assert out.endswith("[truncated]")
    assert count_tokens(out) <= 50
    assert truncate_to_tokens("short", 50) == "short"


def test_run_layout_paths_are_stable(tmp_path: Path) -> None:
    layout = RunLayout(tmp_path)
    assert layout.results_file("s1", "A4", 3) == tmp_path / "s1/A4/results/session_003.jsonl"
    assert layout.snapshot_dir("s1", "A4", 12) == tmp_path / "s1/A4/snapshots/session_012"
    assert layout.status_file("s1", "A0") == tmp_path / "s1/A0/status.json"
    assert ledger_path(tmp_path) == tmp_path / "cost_ledger.sqlite"


def test_fake_transport_replays_script_and_records_calls() -> None:
    transport = FakeTransport([text_reply("a"), text_reply("b")])
    first = transport.chat(
        model="m",
        messages=[{"role": "user", "content": "x"}],
        tools=None,
        reasoning_effort="low",
        max_completion_tokens=10,
    )
    second = transport.chat(
        model="m", messages=[], tools=None, reasoning_effort="low", max_completion_tokens=10
    )
    assert (first.content, second.content) == ("a", "b")
    assert len(transport.calls) == 2
    assert transport.calls[0]["messages"][0]["content"] == "x"


def test_new_fields_default_to_previous_behaviour() -> None:
    # The main run recorded this digest before the follow-up switches existed.
    main = load_experiment(REPO / "configs/experiments/main.yaml")

    assert config_sha256(main).startswith("20233a42b2f0fae0")


@pytest.mark.parametrize(
    "switch",
    [
        {"feedback": "graded"},
        {"session_notes": True},
        {"a2_retrieval": "auto"},
        {"agent_prompt_addendum": "verify_memory"},
        {"consolidator_sql": True},
        {"drift": False},
    ],
)
def test_clbench_rejects_schemadrift_switches(tmp_path: Path, switch: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="SchemaDrift-only"):
        load_experiment(_write_config(tmp_path, task="clbench", **switch))


@pytest.mark.parametrize(
    "phase", ["fu_dev", "fu_pilot", "fu_pilot_rev", "fu_main", "fu_aux", "fu_clbench"]
)
def test_followup_phases_are_valid(tmp_path: Path, phase: str) -> None:
    assert load_experiment(_write_config(tmp_path, phase=phase)).phase == phase


def test_stream_dump_omits_default_drift(tmp_path: Path) -> None:
    stream = Stream(
        stream_id="s",
        seed=1,
        root=tmp_path,
        sessions=12,
        questions_per_session=6,
        questions=[],
        facts=[],
        injections=[],
    )

    assert '"drift"' not in stream.stream_json()
    assert '"drift": false' in stream.model_copy(update={"drift": False}).stream_json()


# --- configs/experiments/followup (docs/followup/00_design.md 5.1, 6, 7, 8) ----------------


@dataclass(frozen=True)
class _Expected:
    phase: str
    arms: list[str]
    stream_ids: list[str]
    seeds: list[int]
    phase_cap_usd: float
    switches: dict[str, object]
    agent_reasoning_effort: str = "low"
    concurrency: int | None = None


def _switches(**changes: object) -> dict[str, object]:
    """The treatment switches of a follow-up run: the fu_fb0 values, changed by `changes`."""
    base: dict[str, object] = {
        "feedback": "none",
        "session_notes": True,
        "a2_retrieval": "auto",
        "agent_prompt_addendum": "none",
        "consolidator_sql": False,
        "drift": True,
    }
    return base | changes


def _stream_ids(prefix: str, first: int, last: int) -> list[str]:
    return [f"{prefix}-{n:02d}" for n in range(first, last + 1)]


_FOLLOWUP_ARMS = ["A0", "A1", "A2", "A4", "A5"]
_VERIFY = _switches(agent_prompt_addendum="verify_memory")
_PROBE = _switches(consolidator_sql=True)
_NULL = _switches(drift=False)


def _run_variants(
    stage: str, phase: str, stream_prefix: str, first_seed: int, cap: float
) -> dict[str, _Expected]:
    """The five runs of one stage (pilot or its revision), keyed by config name."""
    streams = _stream_ids(stream_prefix, 1, 2)
    seeds = [first_seed, first_seed + 1]
    null_streams = [f"{stream_prefix}-null-01"]
    null_seeds = [first_seed + 10]
    return {
        f"fu_{stage}_fb0": _Expected(
            phase, _FOLLOWUP_ARMS, streams, seeds, cap, _switches(feedback="none")
        ),
        f"fu_{stage}_fb1": _Expected(
            phase, ["A0", "A1", "A2", "A4"], streams, seeds, cap, _switches(feedback="graded")
        ),
        f"fu_{stage}_verify": _Expected(phase, ["A0", "A1", "A4"], streams, seeds, cap, _VERIFY),
        f"fu_{stage}_probe": _Expected(phase, ["A4"], streams, seeds, cap, _PROBE),
        f"fu_{stage}_null": _Expected(phase, ["A1", "A4"], null_streams, null_seeds, cap, _NULL),
    }


FOLLOWUP_EXPECTED: dict[str, _Expected] = {
    "fu_dev": _Expected(
        "fu_dev",
        _FOLLOWUP_ARMS,
        _stream_ids("fu-dev", 1, 2),
        [4201, 4202],
        3,
        _switches(
            feedback="graded",
            agent_prompt_addendum="verify_memory",
            consolidator_sql=True,
        ),
    ),
    "fu_dev_null": _Expected("fu_dev", ["A1", "A4"], ["fu-dev-null-01"], [4211], 3, _NULL),
    # E1 re-check of G7 after the consolidator_sql wording fix (30_run-ledger.md).
    "fu_dev_probe": _Expected(
        "fu_dev",
        ["A4"],
        _stream_ids("fu-dev", 1, 2),
        [4201, 4202],
        3,
        _switches(
            feedback="graded",
            agent_prompt_addendum="verify_memory",
            consolidator_sql=True,
        ),
    ),
    **_run_variants("pilot", "fu_pilot", "fu-pilot", 4301, 6),
    **_run_variants("pilot_rev", "fu_pilot_rev", "fu-pilot-rev", 4401, 6),
    "fu_fb0": _Expected(
        "fu_main",
        _FOLLOWUP_ARMS,
        _stream_ids("fu", 1, 40),
        list(range(4001, 4041)),
        52,
        _switches(feedback="none"),
        concurrency=7,
    ),
    "fu_fb1": _Expected(
        "fu_main",
        ["A0", "A1", "A2", "A4"],
        _stream_ids("fu", 1, 40),
        list(range(4001, 4041)),
        52,
        _switches(feedback="graded"),
        concurrency=7,
    ),
    "fu_verify": _Expected(
        "fu_aux",
        ["A0", "A1", "A4"],
        _stream_ids("fu", 1, 20),
        list(range(4001, 4021)),
        25,
        _VERIFY,
        concurrency=2,
    ),
    "fu_probe": _Expected(
        "fu_aux",
        ["A4"],
        _stream_ids("fu", 1, 20),
        list(range(4001, 4021)),
        25,
        _PROBE,
        concurrency=3,
    ),
    "fu_null": _Expected(
        "fu_aux",
        ["A1", "A4"],
        _stream_ids("fu-null", 1, 10),
        list(range(4101, 4111)),
        25,
        _NULL,
        concurrency=3,
    ),
    "fu_clbench": _Expected(
        "fu_clbench",
        ["A0", "A1", "A4"],
        [f"clbench-r{n}" for n in range(3, 18)],
        list(range(3, 18)),
        6,
        {name: ExperimentConfig.model_fields[name].default for name in SCHEMADRIFT_SWITCHES},
        concurrency=2,
    ),
    "fu_sens": _Expected(
        "fu_aux",
        ["A0", "A1", "A4"],
        _stream_ids("fu", 1, 10),
        list(range(4001, 4011)),
        25,
        _switches(),
        agent_reasoning_effort="medium",
    ),
}


def _followup(name: str) -> ExperimentConfig:
    return load_experiment(FOLLOWUP / f"{name}.yaml")


def _without(config: ExperimentConfig, *ignored: str) -> dict[str, object]:
    """The config as a plain dict without the top-level keys `ignored` and the budget cap."""
    dumped = config.model_dump()
    del dumped["budgets"]["phase_cap_usd"]
    for key in ignored:
        del dumped[key]
    return dumped


def test_followup_config_files_are_exactly_the_designed_set() -> None:
    files = sorted(path.name for path in FOLLOWUP.iterdir())

    assert files == sorted(f"{name}.yaml" for name in FOLLOWUP_EXPECTED)


@pytest.mark.parametrize("name", sorted(FOLLOWUP_EXPECTED))
def test_followup_configs_load_and_match_design(name: str) -> None:
    expected = FOLLOWUP_EXPECTED[name]

    config = _followup(name)

    assert config.name == name
    assert (config.phase, config.arms, config.stream_ids, config.seeds) == (
        expected.phase,
        expected.arms,
        expected.stream_ids,
        expected.seeds,
    )
    assert {switch: getattr(config, switch) for switch in SCHEMADRIFT_SWITCHES} == (
        expected.switches
    )
    assert config.arm_overrides == {}
    assert config.budgets.phase_cap_usd == expected.phase_cap_usd
    assert config.budgets.hard_cap_usd == 200.0
    assert config.agent_reasoning_effort == expected.agent_reasoning_effort
    assert config.consolidator_reasoning_effort == "medium"
    assert (config.agent_model, config.consolidator_model) == ("gpt-6-luna", "gpt-6-luna")
    assert config.inject_false_notes is False
    if expected.concurrency is not None:
        assert config.concurrency == expected.concurrency


def test_runs_started_together_share_a_concurrency_budget_of_24() -> None:
    names = ["fu_fb0", "fu_fb1", "fu_verify", "fu_probe", "fu_null", "fu_clbench"]

    assert sum(_followup(name).concurrency for name in names) == 24


@pytest.mark.parametrize(
    "name",
    sorted(n for n, e in FOLLOWUP_EXPECTED.items() if e.switches["drift"] is False),
)
def test_no_drift_runs_use_only_no_drift_streams(name: str) -> None:
    assert all("-null-" in stream_id for stream_id in _followup(name).stream_ids)


@pytest.mark.parametrize("name", sorted(set(FOLLOWUP_EXPECTED) - {"fu_clbench"}))
def test_followup_schemadrift_runs_keep_the_previous_budgets_and_scale(name: str) -> None:
    previous = load_experiment(EXPERIMENTS / "main.yaml")

    config = _followup(name)

    assert config.budgets.model_dump(exclude={"phase_cap_usd"}) == previous.budgets.model_dump(
        exclude={"phase_cap_usd"}
    )
    assert (
        config.sessions,
        config.questions_per_session,
        config.probe_sessions,
        config.probes_per_checkpoint,
        config.embedding_model,
    ) == (
        previous.sessions,
        previous.questions_per_session,
        previous.probe_sessions,
        previous.probes_per_checkpoint,
        previous.embedding_model,
    )


def test_fu_clbench_matches_the_previous_clbench_run_except_streams_and_phase() -> None:
    previous = load_experiment(EXPERIMENTS / "clbench.yaml")

    config = _followup("fu_clbench")

    # Concurrency only schedules jobs; it does not change any result.
    ignored = ("name", "phase", "stream_ids", "seeds", "concurrency")
    assert _without(config, *ignored) == _without(previous, *ignored)
    assert set(config.stream_ids).isdisjoint(previous.stream_ids)


@pytest.mark.parametrize(
    ("fb0_name", "fb1_name"),
    [
        ("fu_fb0", "fu_fb1"),
        ("fu_pilot_fb0", "fu_pilot_fb1"),
        ("fu_pilot_rev_fb0", "fu_pilot_rev_fb1"),
    ],
    ids=["main", "pilot", "pilot_rev"],
)
def test_fb0_fb1_share_streams_and_differ_only_in_feedback(fb0_name: str, fb1_name: str) -> None:
    fb0, fb1 = _followup(fb0_name), _followup(fb1_name)

    assert (fb0.stream_ids, fb0.seeds) == (fb1.stream_ids, fb1.seeds)
    assert (fb0.feedback, fb1.feedback) == ("none", "graded")
    # A5 sees no feedback, so only the no-feedback run holds it (00_design 6).
    assert (fb0.arms, fb1.arms) == (["A0", "A1", "A2", "A4", "A5"], ["A0", "A1", "A2", "A4"])
    assert _without(fb0, "name", "feedback", "arms") == _without(fb1, "name", "feedback", "arms")


@pytest.mark.parametrize("stage", ["fu_pilot", "fu_pilot_rev"])
@pytest.mark.parametrize("label", ["fb0", "fb1", "verify", "probe", "null"])
def test_pilot_configs_mirror_main_except_streams_and_phase(stage: str, label: str) -> None:
    main = _followup(f"fu_{label}")

    pilot = _followup(f"{stage}_{label}")

    ignored = ("name", "phase", "stream_ids", "seeds", "concurrency")
    assert _without(pilot, *ignored) == _without(main, *ignored)
    assert (pilot.phase, main.phase) == (stage, FOLLOWUP_EXPECTED[f"fu_{label}"].phase)
    assert set(pilot.stream_ids).isdisjoint(main.stream_ids)


def test_followup_streams_have_one_seed_each_and_new_seeds() -> None:
    seed_of: dict[str, int] = {}
    for name in FOLLOWUP_EXPECTED:
        config = _followup(name)
        if config.task != "schemadrift":
            continue
        for stream_id, seed in zip(config.stream_ids, config.seeds, strict=True):
            # Streams are generated once and shared by every run that lists them.
            assert seed_of.setdefault(stream_id, seed) == seed, f"{name}: {stream_id}"
    previous = {
        seed
        for path in EXPERIMENTS.glob("*.yaml")
        if (config := load_experiment(path)).task == "schemadrift"
        for seed in config.seeds
    }

    assert len(set(seed_of.values())) == len(seed_of)
    assert set(seed_of.values()).isdisjoint(previous)
    assert all(4000 <= seed < 4500 for seed in seed_of.values())
