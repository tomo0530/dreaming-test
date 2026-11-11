from pathlib import Path

import pytest
import yaml
from fakes import FakeTransport, text_reply
from project_module.config import ConsolidationBudget, ModelSpec, load_experiment, load_models
from project_module.layout import RunLayout, ledger_path
from project_module.manifest import config_sha256
from project_module.schemadrift.models import Fact, Stream
from project_module.tokens import count_tokens, truncate_to_tokens
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[2]


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


@pytest.mark.parametrize("path", sorted((REPO / "configs" / "experiments").glob("*.yaml")))
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
