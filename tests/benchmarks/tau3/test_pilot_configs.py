"""Fixed development selections and matched pilot conditions; no inference."""

from pathlib import Path

import pytest

from safe_agent.benchmarks.tau3.config import load_config
from safe_agent.benchmarks.tau3.inventory import reservation_headroom

CONFIGS = Path(__file__).resolve().parents[3] / "configs" / "benchmarks"
PILOTS = (
    (
        "banking_knowledge",
        "tau3-bank-dev-safe.toml",
        "tau3-bank-dev-baseline.toml",
        ["task_001", "task_002", "task_003", "task_004", "task_005"],
    ),
    (
        "retail",
        "tau3-dev.toml",
        "tau3-dev-baseline.toml",
        ["0", "1", "2", "3", "4"],
    ),
)


@pytest.mark.parametrize("domain,safe_path,baseline_path,task_ids", PILOTS)
def test_pilot_pairs_freeze_selection_exposure_and_conditions(
    domain, safe_path, baseline_path, task_ids
):
    safe = load_config(CONFIGS / safe_path)
    baseline = load_config(CONFIGS / baseline_path)
    assert (safe.system, baseline.system) == ("safe_agent", "llm_agent")
    assert safe.domain == domain
    assert safe.stage == "development"
    assert safe.task_ids == safe.development_task_ids == task_ids
    assert safe.num_tasks == 5
    assert safe.num_trials == 1
    # Task IDs/exposure, models/arguments, seeds, retrieval and every limit match.
    # Only the agent system and its separate artifact identity differ.
    independent_identity = {"system", "run_id", "output_dir"}
    assert safe.model_dump(exclude=independent_identity) == baseline.model_dump(
        exclude=independent_identity
    )


def test_pilot_runs_cannot_share_artifacts_with_each_other_or_final_examples():
    paths = [name for _, safe, baseline, _ in PILOTS for name in (safe, baseline)]
    paths += ["tau3-v1.0.1.toml", "tau3-final.toml", "tau3-final-baseline.toml"]
    configs = [load_config(CONFIGS / name) for name in paths]
    assert len({config.run_id for config in configs}) == len(configs)
    assert len({Path(config.output_dir).resolve() for config in configs}) == len(configs)


@pytest.mark.parametrize("domain,safe_path,baseline_path,task_ids", PILOTS)
def test_pilot_allowances_stay_bounded_and_do_not_claim_to_cap_stock_spend(
    domain, safe_path, baseline_path, task_ids
):
    safe = load_config(CONFIGS / safe_path)
    baseline = load_config(CONFIGS / baseline_path)
    original_development = load_config(CONFIGS / "tau3-dev.toml")
    assert safe.episode_budget == original_development.episode_budget
    assert safe.run_budget == original_development.run_budget
    safe_headroom = reservation_headroom(safe, len(task_ids))
    assert safe_headroom["run_hold_limit_microusd"] == 10_000_000
    assert safe_headroom["attempt_hold_microusd"] == 100_000
    assert safe_headroom["scheduled_matrix_attempt_ceiling_from_holds"] == 100
    assert safe_headroom["mean_attempt_ceiling_per_conversation"] == 20
    assert safe_headroom["invoice_estimate"] is False
    stock_headroom = reservation_headroom(baseline, len(task_ids))
    assert stock_headroom["applies_to_selected_system"] is False
    assert stock_headroom["calculation_available"] is False
    assert "ignored by stock" in stock_headroom["note"]
