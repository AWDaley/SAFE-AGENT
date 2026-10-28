"""Saved launch provenance never promotes fixtures or legacy runs."""

from copy import deepcopy

import pytest

from safe_agent.benchmarks.tau3 import TAU_COMMIT
from safe_agent.benchmarks.tau3.reporting import summarize_saved_results


def manifest():
    return {
        "tau_commit": TAU_COMMIT, "kind": "official_tau3_text", "run_id": "authored",
        "system": "safe_agent", "runtime_mode": "canonical_safe",
        "config": {"num_trials": 1, "model_route": "local",
                   "local_api_base": "http://127.0.0.1:11434/v1"},
        "scheduled": [{"task_id": "authored", "trial": 0}],
        "expected_cost_components": ["agent", "user", "infrastructure"],
        "launch_classification": "qualified_local_live",
        "qualification_gate": {
            "report_sha256": "a" * 64, "review_sha256": "b" * 64,
            "condition_sha256": "sha256:" + "c" * 64,
            "review_is_independent_attestation": False,
        },
    }


@pytest.mark.parametrize("old_optional_hashes", [False, True])
def test_legacy_saved_runs_are_readable_but_never_retroactively_qualified(old_optional_hashes):
    saved = manifest()
    saved.pop("launch_classification")
    if not old_optional_hashes:
        saved.pop("qualification_gate")
    report = summarize_saved_results(saved, [])
    assert report["launch_classification"] == "legacy_unqualified"
    assert report["coverage"]["missing_pairs"] == [("authored", 0)]


@pytest.mark.parametrize("mode", ["injected_development", "custom_development", "stock_baseline"])
@pytest.mark.parametrize("claim_qualified", [False, True])
def test_fixture_and_stock_labels_cannot_be_promoted_by_qualification_metadata(mode, claim_qualified):
    saved = manifest()
    saved["runtime_mode"] = mode
    saved["system"] = "llm_agent" if mode == "stock_baseline" else mode
    expected = "stock_baseline" if mode == "stock_baseline" else "development_fixture"
    saved["launch_classification"] = "qualified_local_live" if claim_qualified else expected
    report = summarize_saved_results(saved, [])
    assert report["launch_classification"] == expected


@pytest.mark.parametrize("mode", ["injected_development", "custom_development"])
def test_old_fixture_labels_survive_missing_classification(mode):
    saved = manifest()
    saved.pop("launch_classification")
    saved["runtime_mode"] = saved["system"] = mode
    assert summarize_saved_results(saved, [])["launch_classification"] == "development_fixture"


def test_valid_recorded_qualification_is_explicitly_saved_metadata_without_rejudging():
    saved = manifest()
    original = deepcopy(saved)
    report = summarize_saved_results(saved, [])
    assert report["launch_classification"] == "qualified_local_live"
    assert "not revalidated" in report["launch_classification_basis"]
    assert "no independent model" in report["launch_classification_basis"]
    assert saved == original


@pytest.mark.parametrize("change", [
    "missing_gate", "malformed_hash", "independent_claim", "private_text", "hosted", "missing_mode",
])
def test_inconsistent_recorded_qualified_classification_is_rejected(change):
    saved = manifest()
    if change == "missing_gate":
        saved.pop("qualification_gate")
    elif change == "malformed_hash":
        saved["qualification_gate"]["report_sha256"] = "not-a-digest"
    elif change == "independent_claim":
        saved["qualification_gate"]["review_is_independent_attestation"] = True
    elif change == "private_text":
        saved["qualification_gate"]["reviewer"] = "PRIVATE_REVIEWER"
    elif change == "hosted":
        saved["config"].update(model_route="openai", local_api_base=None)
    else:
        saved.pop("runtime_mode")
    with pytest.raises(ValueError, match="^RECORDED_LAUNCH_CLASSIFICATION_INVALID$"):
        summarize_saved_results(saved, [])


def test_hosted_launch_does_not_acquire_local_qualification():
    saved = manifest()
    saved["config"].update(model_route="openai", local_api_base=None)
    saved["launch_classification"] = "unqualified_hosted_live"
    saved.pop("qualification_gate")
    assert summarize_saved_results(saved, [])["launch_classification"] == "unqualified_hosted_live"
