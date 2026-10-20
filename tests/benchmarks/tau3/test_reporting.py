import pytest

from safe_agent.benchmarks.tau3 import TAU_COMMIT
from safe_agent.benchmarks.tau3.reporting import pass_hat_k, summarize_saved_results


def manifest():
    return {
        "tau_commit": TAU_COMMIT,
        "kind": "official_tau3_text",
        "run_id": "test_run",
        "system": "safe_agent",
        "config": {"num_trials": 4},
        "scheduled": [
            {"task_id": task, "trial": trial} for task in ("a", "b") for trial in range(4)
        ],
        "expected_cost_components": ["agent", "user", "judge"],
    }


def simulations():
    return [
        {
            "id": f"{task}_{trial}",
            "task_id": task,
            "trial": trial,
            "termination_reason": "user_stop",
            "reward_info": {"reward": 1.0 if task == "a" or trial < 2 else 0.0},
            "agent_cost": 0.1,
            "user_cost": 0.05,
        }
        for task in ("a", "b")
        for trial in range(4)
    ]


def test_all_k_success_estimator_and_recorded_costs():
    report = summarize_saved_results(manifest(), simulations())
    assert pass_hat_k(4, 3, 1) == 0.75
    assert pass_hat_k(4, 3, 4) == 0
    assert report["saved_reward_metrics"]["pass_hat_ks"]["1"] == 0.75
    assert report["saved_reward_metrics"]["pass_hat_ks"]["4"] == 0.5
    assert report["coverage"]["matrix_complete"]
    assert report["costs"]["known_total_usd"] == "1.20"
    assert report["costs"]["unreported_components"] == ["judge"]
    assert not report["costs"]["complete"]


def test_missing_and_infrastructure_failures_stay_in_operational_denominator():
    rows = simulations()[1:]
    rows[-1].update(termination_reason="infrastructure_error", reward_info=None)
    report = summarize_saved_results(manifest(), rows)
    assert report["coverage"]["missing_pairs"] == [("a", 0)]
    assert report["coverage"]["infrastructure_failures"] == 1
    assert "4" not in report["saved_reward_metrics"]["pass_hat_ks"]
    assert report["conservative_operational_metrics"]["pass_hat_ks"]["4"] == 0
    assert not report["coverage"]["matrix_complete"]


def test_duplicate_attempts_invalidate_score_without_erasing_their_cost():
    rows = simulations()
    rows.append(dict(rows[0], id="retry"))
    report = summarize_saved_results(manifest(), rows)
    assert report["coverage"]["duplicate_pairs"] == [("a", 0)]
    assert not report["saved_reward_metrics"]["input_valid"]
    assert report["costs"]["known_total_usd"] == "1.35"


def test_zero_success_cost_is_undefined_and_unknown_is_not_free():
    rows = simulations()
    for row in rows:
        row["reward_info"] = {"reward": 0.0}
    rows[0]["agent_cost"] = None
    report = summarize_saved_results(manifest(), rows)
    assert report["costs"]["known_agent_operating_cost_per_success_usd"] is None
    assert report["costs"]["missing_cost_entries"]["agent"] == 1


def test_rejects_fixture_scores_wrong_revision_and_double_billing():
    with pytest.raises(ValueError, match="fixtures"):
        summarize_saved_results(dict(manifest(), kind="scripted_fixture"), [])
    with pytest.raises(ValueError, match="revision"):
        summarize_saved_results(dict(manifest(), tau_commit="other"), [])
    with pytest.raises(ValueError, match="double-count"):
        summarize_saved_results(
            manifest(), simulations(), [{"category": "agent", "amount_usd": "1"}]
        )


def test_safe_reservation_holds_are_not_billed_spend():
    run_manifest = manifest()
    run_manifest["expected_cost_components"].append("agent_provider_billing")
    rows = simulations()
    rows[0]["messages"] = [
        {
            "role": "assistant",
            "usage": {
                "safe_model_attempts": 2,
                "safe_reserved_cost_microusd": 200_000,
                "input_tokens": 20,
                "output_tokens": 10,
                "safe_usage_source": ["scripted", "scripted"],
                "safe_billing_verified": False,
            },
        }
    ]
    report = summarize_saved_results(run_manifest, rows)
    assert report["costs"]["known_total_usd"] == "1.20"
    assert "agent_provider_billing" in report["costs"]["unreported_components"]
    assert not report["costs"]["complete"]
    usage = report["safe_inference_recorded_usage"]
    assert usage["safe_model_attempts"] == 2
    assert usage["safe_reserved_cost_microusd"] == 200_000
    assert usage["sources"] == {"scripted": 2}
    assert not usage["reserved_cost_is_actual_spend"]
    assert not usage["billing_verified"]
