from pathlib import Path

from safe_agent.benchmarks.tau3 import TAU_COMMIT, inventory
from safe_agent.benchmarks.tau3.config import BenchmarkConfig, load_config


def test_source_verification_detects_tampering_and_extra_task(tmp_path, monkeypatch):
    relative = "data/tau2/domains/banking_knowledge/tasks/task_001.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"id":"task_001"}')
    digest = inventory.git_blob_hash(path.read_bytes())
    monkeypatch.setattr(
        inventory,
        "pinned_source_manifest",
        lambda: {
            "commit": TAU_COMMIT,
            "files": {relative: digest},
        },
    )
    assert inventory.verify_sources(tmp_path, "banking_knowledge")["verified"]
    path.write_bytes(b'{"id":"task_999"}')
    assert inventory.verify_sources(tmp_path, "banking_knowledge")["modified_files"] == [relative]
    path.write_bytes(b'{"id":"task_001"}')
    extra = path.with_name("task_002.json")
    extra.write_text("{}")
    assert inventory.verify_sources(tmp_path, "banking_knowledge")["unexpected_task_files"]


def test_missing_checkout_does_not_claim_observed_task_counts(tmp_path):
    report = inventory.inspect_benchmark(BenchmarkConfig(), tmp_path / "missing")
    assert report["status"] == "missing_source"
    assert report["expected_domain_task_count"] == 97
    assert "domain_task_count" not in report
    assert report["model_calls"] == 0


def test_bundled_pin_is_complete_and_excludes_voice_task_inputs():
    pin = inventory.pinned_source_manifest()
    assert pin["commit"] == TAU_COMMIT
    tasks = [path for path in pin["files"] if "/banking_knowledge/tasks/task_" in path]
    assert len(tasks) == 97
    assert not any(Path(path).name == "tasks_voice.json" for path in pin["files"])


def test_example_hold_headroom_is_visible_and_stock_budget_is_not_claimed():
    coverage = load_config("configs/benchmarks/tau3-v1.0.1.toml")
    final = load_config("configs/benchmarks/tau3-final.toml")
    baseline = load_config("configs/benchmarks/tau3-final-baseline.toml")
    limited = inventory.reservation_headroom(coverage, 97)
    assert limited["episode_attempt_ceiling_from_holds"] == 100
    assert limited["scheduled_matrix_attempt_ceiling_from_holds"] == 100
    assert limited["mean_attempt_ceiling_per_conversation"] == 100 / 97
    expanded = inventory.reservation_headroom(final, 388)
    assert expanded["scheduled_matrix_attempt_ceiling_from_holds"] == 1000
    assert expanded["mean_attempt_ceiling_per_conversation"] == 1000 / 388
    assert not expanded["invoice_estimate"]
    stock = inventory.reservation_headroom(baseline, 388)
    assert not stock["applies_to_selected_system"]
    assert not stock["calculation_available"]
    assert "ignored by stock" in stock["note"]
