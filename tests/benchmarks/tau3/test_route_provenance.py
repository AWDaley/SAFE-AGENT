"""Authored route records only; no local server, hosted call, or measured scores."""

import hashlib
import json

import pytest
import test_calibration as calibration_fixtures
import test_submission as submission_fixtures
from test_comparison import _change, _fixture
from test_reporting import manifest as report_manifest
from test_reporting import simulations as report_simulations

from safe_agent.benchmarks.tau3 import calibration
from safe_agent.benchmarks.tau3.calibration import calibrate_pilot
from safe_agent.benchmarks.tau3.comparison import ComparisonError, compare_saved_runs
from safe_agent.benchmarks.tau3.config import BenchmarkConfig, harness_model_settings
from safe_agent.benchmarks.tau3.inventory import content_hash
from safe_agent.benchmarks.tau3.reporting import cost_accounting, summarize_saved_results

LOCAL_BASE = "http://127.0.0.1:11434/v1"
authored_pilot = calibration_fixtures.authored_pilot
saved_run = submission_fixtures.saved_run


def _write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, allow_nan=False))


def _declare_local(path, *, base=LOCAL_BASE):
    manifest = json.loads((path / "manifest.json").read_text())
    results = json.loads((path / "results.json").read_text())
    manifest["config"].update(model_route="local", local_api_base=base)
    config = BenchmarkConfig.model_validate(manifest["config"])
    manifest["config_sha256"] = content_hash(manifest["config"])
    manifest["inventory"]["selected_tasks_with_nl_assertions"] = 0
    manifest["expected_cost_components"] = ["agent", "user", "infrastructure"]
    for role in ("agent", "user"):
        model, arguments = harness_model_settings(config, role)
        manifest["official_config"]["llm_" + role] = model
        manifest["official_config"]["llm_args_" + role] = arguments
        results["info"][role + "_info"].update(llm=model, llm_args=arguments)
    _write(path / "results.json", results)
    if "saved_artifacts_sha256" in manifest:
        manifest["saved_artifacts_sha256"] = {
            name: hashlib.sha256((path / name).read_bytes()).hexdigest()
            for name in ("results.json", "official_metrics.json")
        }
    _write(path / "manifest.json", manifest)


def _declare_legacy(path):
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["config"].pop("model_route", None)
    manifest["config"].pop("local_api_base", None)
    manifest["config_sha256"] = content_hash(manifest["config"])
    _write(path / "manifest.json", manifest)


def test_legacy_hosted_config_matches_explicit_hosted_without_rehashing_defaults(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _declare_legacy(left)
    report = compare_saved_runs(left, right, bootstrap_samples=100)
    assert report["shared_condition"]["model_route"] == "openai"
    assert report["shared_condition"]["local_api_base"] is None
    assert report["left"]["provenance"]["model_route_defaulted_from_legacy_config"]
    assert not report["right"]["provenance"]["model_route_defaulted_from_legacy_config"]
    _change(left, "manifest.json", lambda value: value.update(
        config_sha256=content_hash(BenchmarkConfig.model_validate(value["config"]).model_dump())
    ))
    with pytest.raises(ComparisonError, match="^CONFIG_HASH_MISMATCH$"):
        compare_saved_runs(left, right)


def test_local_safe_sidecar_and_stock_harness_compare_under_one_declared_condition(tmp_path):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _declare_local(left)
    _declare_local(right)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    report = compare_saved_runs(left, right, bootstrap_samples=100)
    condition = report["shared_condition"]
    assert condition["model_route"] == "local" and condition["local_api_base"] == LOCAL_BASE
    assert not condition["model_weights_or_hardware_attested"]
    assert report["matched_metadata_valid"]
    assert report["paired_cost_usd_per_scheduled_conversation"]["paired_tasks"] == 0
    assert "infrastructure" in report["left"]["report"]["costs"]["unreported_components"]
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


@pytest.mark.parametrize("condition", ["route", "endpoint"])
def test_route_or_endpoint_mismatch_cannot_silently_form_a_pair(tmp_path, condition):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _declare_local(left)
    if condition == "endpoint":
        _declare_local(right, base="http://127.0.0.1:12345/v1")
    with pytest.raises(ComparisonError, match="^CONDITION_MISMATCH_(MODEL_ROUTE|LOCAL_API_BASE)$"):
        compare_saved_runs(left, right)


@pytest.mark.parametrize("field", ["model", "endpoint", "key", "retries", "result_model"])
def test_local_effective_harness_metadata_must_match_declared_route(tmp_path, field):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _declare_local(left)
    _declare_local(right)
    if field == "result_model":
        _change(right, "results.json", lambda value: value["info"]["agent_info"].update(llm="changed"))
    elif field == "model":
        _change(right, "manifest.json", lambda value: value["official_config"].update(llm_agent="changed"))
    else:
        key, value = {
            "endpoint": ("api_base", "http://127.0.0.1:12345/v1"),
            "key": ("api_key", "UNEXPECTED_AUTHORED_SECRET"),
            "retries": ("num_retries", 1),
        }[field]
        _change(right, "manifest.json", lambda data: data["official_config"]["llm_args_agent"].update({key: value}))
    with pytest.raises(ComparisonError, match="^(OFFICIAL_CONFIG|RESULTS_MODEL)_MISMATCH$"):
        compare_saved_runs(left, right)


@pytest.mark.parametrize("nl_count", [None, False, 1])
def test_local_saved_condition_requires_explicit_integer_zero_nl_assertions(tmp_path, nl_count):
    left, right = _fixture(tmp_path, "safe_agent"), _fixture(tmp_path, "llm_agent")
    _declare_local(left)
    _declare_local(right)
    _change(left, "manifest.json", lambda value: value["inventory"].update(
        selected_tasks_with_nl_assertions=nl_count
    ))
    with pytest.raises(ComparisonError, match="^LOCAL_ZERO_NL_ASSERTION_SCHEDULE_REQUIRED$"):
        compare_saved_runs(left, right)


def test_local_zero_upstream_prices_do_not_establish_zero_operating_costs():
    manifest = report_manifest()
    manifest["config"].update(model_route="local", local_api_base=LOCAL_BASE)
    # Even a missing expected-infrastructure declaration cannot imply free compute.
    manifest["expected_cost_components"] = ["agent", "user"]
    rows = report_simulations()
    for row in rows:
        row.update(agent_cost=0, user_cost=0)
    report = summarize_saved_results(manifest, rows)
    costs = report["costs"]
    assert report["model_route_provenance"]["model_route"] == "local"
    assert costs["known_total_usd"] == "0" and not costs["complete"]
    assert costs["unreported_components"] == ["infrastructure"]
    assert "do not measure hardware or electricity" in costs["upstream_cost_basis"]
    assert not costs["local_hardware_costs_independently_verified"]
    assert "excludes infrastructure" in costs["agent_operating_numerator_scope"]
    costs, pairs = cost_accounting(manifest, rows, [{"category": "infrastructure", "amount_usd": "2"}])
    assert costs["complete"] and costs["known_total_usd"] == "2"
    assert all(not pair["complete"] for pair in pairs.values())


def _local_calibration(authored_pilot, monkeypatch):
    path, target, held = authored_pilot
    _declare_local(path)
    inspect = calibration.inspect_benchmark
    monkeypatch.setattr(calibration, "inspect_benchmark", lambda config, root=None: {
        **inspect(config, root), "selected_tasks_with_nl_assertions": 0,
    })
    return path, target.model_copy(update={
        "model_route": "local", "local_api_base": LOCAL_BASE,
        "stage": "development", "task_ids": ["0", "1"],
    }), held


def test_local_calibration_keeps_held_usage_and_disclaims_weight_and_price_attestation(authored_pilot, monkeypatch):
    path, target, held = _local_calibration(authored_pilot, monkeypatch)
    report = calibrate_pilot(path, target)
    assert report["status"] == "review_required"
    assert report["target_model_route"] == report["provenance"]["model_route"] == "local"
    assert report["target_local_api_base"] == report["provenance"]["local_api_base"] == LOCAL_BASE
    assert report["per_episode_usage"]["tokens"]["sum"] == sum(held)
    assert report["derived_requirements"]["unchanged_attempt_hold_microusd"] == 7
    assert not report["model_weights_or_hardware_attested"] and not report["invoice_estimate"]


@pytest.mark.parametrize("condition", ["route", "endpoint"])
def test_local_calibration_never_extrapolates_to_another_route_or_endpoint(authored_pilot, monkeypatch, condition):
    path, target, _ = _local_calibration(authored_pilot, monkeypatch)
    updates = ({"model_route": "openai", "local_api_base": None} if condition == "route"
               else {"local_api_base": "http://127.0.0.1:12345/v1"})
    report = calibrate_pilot(path, target.model_copy(update=updates))
    assert report["status"] == "unavailable"
    assert "PILOT_CONDITION_MISMATCH" in report["reasons"]
    assert report["suggested_budget_patch"] is None


@pytest.mark.parametrize("nl_count", [None, False, 1])
def test_local_calibration_target_with_unknown_or_hosted_judge_need_has_no_suggestion(authored_pilot, monkeypatch, nl_count):
    path, target, _ = _local_calibration(authored_pilot, monkeypatch)
    inspect = calibration.inspect_benchmark
    monkeypatch.setattr(calibration, "inspect_benchmark", lambda config, root=None: {
        **inspect(config, root),
        "selected_tasks_with_nl_assertions": nl_count if config.run_id == target.run_id else 0,
    })
    report = calibrate_pilot(path, target)
    assert report["status"] == "unavailable"
    assert "TARGET_LOCAL_ZERO_NL_ASSERTION_SCHEDULE_REQUIRED" in report["reasons"]
    assert report["suggested_budget_patch"] is None


def test_legacy_pilot_raw_hash_and_unchanged_run_policy_remain_readable(authored_pilot):
    path, target, _ = authored_pilot
    _declare_legacy(path)
    report = calibrate_pilot(path, target)
    assert report["status"] == "review_required"
    assert report["provenance"]["model_route"] == "openai"
    assert report["provenance"]["model_route_defaulted_from_legacy_config"]


def test_legacy_final_submission_retains_raw_config_hash(saved_run):
    config = saved_run.manifest["config"]
    config.pop("model_route", None)
    config.pop("local_api_base", None)
    original_hash = content_hash(config)
    saved_run.manifest["config_sha256"] = original_hash
    saved_run.save()
    assert saved_run.prepare()["status"] == "validated_saved_records"
    disclosures = json.loads((saved_run.output_dir / "disclosures.json").read_text())
    assert disclosures["config_sha256"] == original_hash
    assert disclosures["config"]["model_route"] == "openai"
    assert disclosures["model_route_defaulted_from_legacy_config"]


def test_local_run_cannot_be_relabelled_for_final_submission(saved_run):
    saved_run.manifest["config"].update(model_route="local", local_api_base=LOCAL_BASE)
    saved_run.manifest["config_sha256"] = content_hash(saved_run.manifest["config"])
    saved_run.save()
    saved_run.reject("^HOSTED_FINAL_SUBMISSION_REQUIRED$")
