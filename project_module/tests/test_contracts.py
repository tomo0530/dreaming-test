from pathlib import Path

import pytest
import yaml
from fakes import FakeTransport, text_reply
from project_module.config import ConsolidationBudget, load_experiment, load_models
from project_module.layout import RunLayout, ledger_path
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


def test_summarizer_turns_equal_analyzer_plus_organizer() -> None:
    assert ConsolidationBudget().summarizer_max_turns == 22


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
