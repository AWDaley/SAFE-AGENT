import pytest

from safe_agent.benchmarks.tau3 import TAU_COMMIT
from safe_agent.benchmarks.tau3.reporting import (
    cost_accounting,
    pass_hat_k,
    summarize_saved_results,
)


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


def reviewed_agent_invoice():
    return {
        "category": "agent_provider_billing",
        "run_id": "test_run",
        "component": "agent",
        "currency": "USD",
        "scope": "all_agent_attempts_including_failures_and_repairs",
        "replaces": "upstream_agent_aggregate",
        "amount_usd": "2.00",
        "event_id": "authored-invoice-total",
        "provider": "authored-provider",
        "source_sha256": "a" * 64,
        "reviewer": "PRIVATE_REVIEWER_CANARY",
        "allocations": [{**pair, "amount_usd": "0.25"} for pair in manifest()["scheduled"]],
    }


def test_reviewed_invoice_replaces_the_upstream_agent_aggregate_without_double_counting():
    run_manifest = manifest()
    run_manifest["expected_cost_components"].append("agent_provider_billing")
    rows = simulations()
    report = summarize_saved_results(run_manifest, rows, [reviewed_agent_invoice()])
    costs = report["costs"]
    assert costs["known_total_usd"] == "2.40"
    assert costs["upstream_agent_reported_usd"] == "0.8"
    assert "agent" not in costs["known_usd_by_category"]
    assert costs["known_usd_by_category"]["agent_provider_billing"] == "2.00"
    assert costs["unreported_components"] == ["judge"]
    assert not costs["complete"]
    assert costs["agent_provider_reconciliation"]["upstream_agent_amount_replaced"]
    assert not costs["agent_provider_reconciliation"]["provider_attestation_verified"]
    assert "PRIVATE_REVIEWER_CANARY" not in str(report)
    rows[0]["agent_cost"] = None
    complete = summarize_saved_results(
        run_manifest,
        rows,
        [
            reviewed_agent_invoice(),
            {"category": "judge", "amount_usd": "0", "event_id": "authored-judge-zero"},
        ],
    )
    assert complete["costs"]["complete"]
    assert "agent" not in complete["costs"]["missing_cost_entries"]


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "another-run"},
        {"currency": "EUR"},
        {"component": "user"},
        {"scope": "successful_attempts_only"},
        {"source_sha256": "not_a_digest"},
    ],
)
def test_invoice_requires_exact_run_component_currency_scope_and_provenance(changes):
    with pytest.raises(ValueError, match="AGENT_BILLING"):
        summarize_saved_results(
            manifest(), simulations(), [dict(reviewed_agent_invoice(), **changes)]
        )


def test_invoice_cannot_duplicate_totals_or_absorb_ambiguous_extra_agent_charges():
    for extra in (
        reviewed_agent_invoice(),
        {"category": "agent_extra", "amount_usd": "0.2"},
        {"category": "failed_request", "amount_usd": "0.2"},
    ):
        with pytest.raises(ValueError, match="AGENT_BILLING"):
            summarize_saved_results(manifest(), simulations(), [reviewed_agent_invoice(), extra])


def test_invoice_allocations_must_cover_every_pair_and_equal_the_run_total():
    event = reviewed_agent_invoice()
    event["allocations"][0]["amount_usd"] = "0.30"
    with pytest.raises(ValueError, match="ALLOCATIONS"):
        summarize_saved_results(manifest(), simulations(), [event])
    event = reviewed_agent_invoice()
    event["allocations"].pop()
    with pytest.raises(ValueError, match="ALLOCATIONS"):
        summarize_saved_results(manifest(), simulations(), [event])


@pytest.mark.parametrize("condition", ["missing", "duplicate", "ungraded", "infrastructure"])
def test_cost_ratio_is_not_a_lower_bound_with_an_unfixed_success_denominator(condition):
    assert summarize_saved_results(manifest(), simulations())["costs"][
        "cost_per_success_is_lower_bound"
    ]
    rows = simulations()
    if condition == "missing":
        rows.pop()
    elif condition == "duplicate":
        rows.append(dict(rows[0]))
    elif condition == "ungraded":
        rows[0]["reward_info"] = None
    else:
        rows[0].update(termination_reason="infrastructure_error", reward_info=None)
    costs = summarize_saved_results(manifest(), rows)["costs"]
    assert not costs["successful_denominator_complete"]
    assert not costs["cost_per_success_is_lower_bound"]
    assert (
        costs["cost_per_success_denominator"]
        == "observed_valid_successes_in_incomplete_outcome_matrix"
    )


def test_failed_agent_and_retrieval_charges_are_in_the_operating_numerator():
    run_manifest = dict(
        manifest(),
        scheduled=[{"task_id": "a", "trial": 0}],
        expected_cost_components=["agent", "user"],
    )
    row = dict(simulations()[0], agent_cost=1, user_cost=0)
    costs, _ = cost_accounting(
        run_manifest,
        [row],
        [
            {"category": "failed_request", "component": "agent", "amount_usd": "5"},
            {"category": "failed_request", "component": "retrieval", "amount_usd": "2"},
        ],
    )
    assert costs["known_total_usd"] == "8"
    assert costs["known_agent_operating_cost_per_success_usd"] == "8"
    assert costs["complete"] and costs["operating_cost_attribution_complete"]
    ambiguous, _ = cost_accounting(
        run_manifest, [row], [{"category": "failed_request", "amount_usd": "5"}]
    )
    assert not ambiguous["operating_cost_attribution_complete"]
    assert ambiguous["cost_per_success_is_lower_bound"]


def test_unallocated_cost_in_an_already_allocated_category_keeps_task_totals_incomplete():
    pairs = [{"task_id": task, "trial": 0} for task in ("a", "b")]
    run_manifest = dict(manifest(), scheduled=pairs)
    rows = [
        dict(simulations()[index], task_id=pair["task_id"], trial=0, agent_cost=1, user_cost=0)
        for index, pair in enumerate(pairs)
    ]
    events = [{"category": "judge", "amount_usd": "1", **pair} for pair in pairs]
    events.append({"category": "judge", "amount_usd": "100"})
    costs, allocations = cost_accounting(run_manifest, rows, events)
    assert costs["known_total_usd"] == "104"
    assert costs["unallocated_additional_components"] == ["judge"]
    assert costs["complete"]
    for item in allocations.values():
        assert item["known_total_usd"] == "2"
        assert not item["complete"]
        assert item["unattributed_or_unknown_components"] == ["judge"]
