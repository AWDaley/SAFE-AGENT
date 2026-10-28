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


def test_qualification_hashes_are_saved_before_any_official_import_or_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: {
        "status": "ready", "selected_task_ids": ["authored"],
        "selected_tasks_with_nl_assertions": 0,
    })
    monkeypatch.setattr(runner, "_repository_revision", lambda: {"commit": "authored"})
    output = tmp_path / "gated"
    evidence = {"report_sha256": "a" * 64, "review_sha256": "b" * 64,
                "condition_sha256": "sha256:" + "c" * 64,
                "review_is_independent_attestation": False}

    def stop_at_import(root):
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["qualification_gate"] == evidence
        assert json.loads((output / "qualification-gate.json").read_text()) == evidence
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "prepare_official_import", stop_at_import)
    with pytest.raises(KeyboardInterrupt):
        runner.run_official(BenchmarkConfig(output_dir=str(output)), tau_root=tmp_path,
                            runtime_factory=lambda context: None, qualification_evidence=evidence)
    assert json.loads((output / "manifest.json").read_text())["status"] == "interrupted"
    assert stat.S_IMODE((output / "qualification-gate.json").stat().st_mode) == 0o600


def test_qualification_gate_rejects_extra_private_review_text_before_preparation(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: pytest.fail("Must reject before inspection"))
    evidence = {"report_sha256": "a" * 64, "review_sha256": "b" * 64,
                "condition_sha256": "sha256:" + "c" * 64,
                "review_is_independent_attestation": False, "review_text": "PRIVATE_REVIEW"}
    with pytest.raises(ValueError, match="QUALIFICATION_GATE_METADATA_INVALID"):
        runner.run_official(BenchmarkConfig(output_dir=str(tmp_path / "not_created")),
                            qualification_evidence=evidence)
    assert not (tmp_path / "not_created").exists()


def test_absent_stop_flag_preserves_default_pinned_batch_dispatch(monkeypatch, tmp_path):
    from types import SimpleNamespace

    pytest.importorskip("tau2")
    import tau2.metrics.agent_metrics
    import tau2.runner

    from safe_agent.benchmarks.tau3 import execution

    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: {
        "status": "ready", "selected_task_ids": ["authored"],
        "selected_tasks_with_nl_assertions": 0,
    })
    monkeypatch.setattr(runner, "_repository_revision", lambda: {"commit": "authored"})
    monkeypatch.setattr(runner, "prepare_official_import", lambda *args: None)
    monkeypatch.setattr("safe_agent.benchmarks.tau3.preflight.preflight_run",
                        lambda *args, **kwargs: {"ready_for_attempt": True})
    output = tmp_path / "default_batch"
    calls = []

    def pinned_batch(config):
        calls.append(config)
        (output / "results.json").write_text('{"simulations": []}')
        return object()

    monkeypatch.setattr(tau2.runner, "run_domain", pinned_batch)
    monkeypatch.setattr(execution, "run_controlled", lambda *args, **kwargs: pytest.fail("Controlled scheduling requires explicit opt-in"))
    monkeypatch.setattr(tau2.metrics.agent_metrics, "compute_metrics",
                        lambda results: SimpleNamespace(model_dump=lambda: {"authored_fixture": True}))
    config = BenchmarkConfig(system="llm_agent", output_dir=str(output))
    assert runner.run_official(config, tau_root=tmp_path) == output
    assert len(calls) == 1
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["scheduler"] == "pinned_upstream_batch"
    assert "episode_index" not in manifest and "qualification_gate" not in manifest
    assert not (output / "private/episode-index.json").exists()
    assert not (output / "qualification-gate.json").exists()
