"""Runner provenance and output protections without entering a model runner."""

import json
import os
import stat

import pytest

from safe_agent.benchmarks.tau3 import runner
from safe_agent.benchmarks.tau3.config import BenchmarkConfig


def test_final_run_rejects_injected_runtime_before_inspection(monkeypatch, tmp_path):
    def do_not_inspect(*args, **kwargs):
        raise AssertionError("Invalid final configuration must fail before preparation")

    monkeypatch.setattr(runner, "inspect_benchmark", do_not_inspect)
    config = BenchmarkConfig(stage="final", num_trials=4, output_dir=str(tmp_path / "run"))
    with pytest.raises(ValueError, match="development"):
        runner.run_official(config, runtime_factory=lambda context: None)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("injected", [False, True])
def test_failed_preparation_keeps_private_artifacts_and_restores_umask(
    monkeypatch, tmp_path, injected
):
    monkeypatch.setattr(
        runner,
        "inspect_benchmark",
        lambda *args: {
            "status": "ready",
            "selected_task_ids": ["authored_fixture"],
            "selected_tasks_with_nl_assertions": 0,
        },
    )
    monkeypatch.setattr(runner, "_repository_revision", lambda: {"commit": "authored_fixture"})
    monkeypatch.setattr(
        "safe_agent.benchmarks.tau3.preflight.preflight_run",
        lambda *args, **kwargs: {"ready_for_attempt": True, "authored_test": True},
    )

    def stop_before_import(*args):
        raise RuntimeError("Authored failure before any official runner or model call")

    monkeypatch.setattr(runner, "prepare_official_import", stop_before_import)
    output = tmp_path / "run"
    config = BenchmarkConfig(output_dir=str(output))
    original_umask = os.umask(0o027)
    try:
        with pytest.raises(RuntimeError, match="before any"):
            runner.run_official(
                config,
                tau_root=tmp_path,
                runtime_factory=(lambda context: None) if injected else None,
            )
        restored_umask = os.umask(0o027)
        assert restored_umask == 0o027
    finally:
        os.umask(original_umask)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    path = output / "manifest.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    manifest = json.loads(path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["error_type"] == "RuntimeError"
    expected = "injected_development" if injected else "canonical_safe"
    assert manifest["runtime_mode"] == expected
    assert manifest["system"] == ("injected_development" if injected else "safe_agent")
    assert (
        "external billed spend is not independently capped/verified"
        in manifest["billing_cap_scope"]
    )
