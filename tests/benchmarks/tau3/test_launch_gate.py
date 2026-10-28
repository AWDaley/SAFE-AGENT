"""Shared launch controls with authored files; no fixture is model evidence."""

import hashlib
import json
import stat
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from safe_agent.benchmarks.tau3 import __main__ as cli
from safe_agent.benchmarks.tau3 import preflight, qualification, runner
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.contracts.episode_inference import LocalTimingProfile, wire_digest


def local_config(tmp_path, **changes):
    values = {
        "model_route": "local", "local_api_base": "http://127.0.0.1:11434/v1",
        "llm_agent": "authored-local-model", "llm_user": "authored-local-model",
        "output_dir": str(tmp_path / "benchmark"), "task_ids": ["authored_task"],
        "development_task_ids": ["authored_task"],
        "local_timing": LocalTimingProfile(
            provider_timeout_seconds=90.0, transport_timeout_seconds=105.0,
            request_timeout_seconds=115.0,
        ),
    }
    return BenchmarkConfig(**{**values, **changes})


@pytest.fixture
def authored_launch(tmp_path, monkeypatch):
    """Construct a full integrity fixture, without mocking the launch validator."""
    config = local_config(tmp_path)
    monkeypatch.setattr(runner, "_repository_revision", lambda: {
        "runtime_sources_sha256": "sha256:" + "a" * 64,
    })
    condition = qualification.qualification_condition(config)
    budgets = qualification._qualification_budgets(config, 3600)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    probes = []
    for probe in qualification.probe_schedule():
        trace_hash = qualification._new_json(private / f"{probe.probe_id}.json", {
            "authored_unit_test_fixture": True, "probe_id": probe.probe_id,
        })
        probes.append({
            **asdict(probe), "passed": True, "issues": [], "trace_sha256": trace_hash,
            "requests": [{
                "role": probe.role, "status": "COMPLETE", "seconds": 10.0,
                "input_tokens": 4500, "output_tokens": 50,
            }],
        })
    report = {
        "schema_version": 1, "kind": "authored_local_qualification",
        "profile": qualification.PROFILE, "condition": condition,
        "condition_sha256": wire_digest(condition), "max_seconds": 3600,
        "effective_qualification_budgets": budgets,
        "effective_qualification_budgets_sha256": wire_digest(budgets),
        "probes": probes, "finished_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        **qualification.summarize(probes, scheduled=24, deadline_seconds=90),
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps({
        "schema_version": 1, "approved": True,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "reviewed_probe_ids": [probe["probe_id"] for probe in probes],
        "reviewer": "AUTHORED_PRIVATE_REVIEWER",
        "reviewed_at": datetime.now(UTC).isoformat(),
    }))
    return config, report_path, review_path


@pytest.mark.parametrize("entrypoint", ["runner", "cli"])
@pytest.mark.parametrize(("case", "reason"), [
    ("missing_report", "QUALIFICATION_REPORT_REQUIRED"),
    ("missing_review", "QUALIFICATION_SEMANTIC_REVIEW_REQUIRED"),
    ("missing_report_file", "QUALIFICATION_EVIDENCE_INVALID"),
    ("failed", "QUALIFICATION_NOT_PASSED"),
    ("running", "QUALIFICATION_NOT_PASSED"),
    ("incomplete", "QUALIFICATION_COVERAGE_INCOMPLETE"),
    ("altered_report", "QUALIFICATION_REVIEW_INVALID"),
    ("altered_trace", "QUALIFICATION_TRACE_INVALID"),
    ("rejected_review", "QUALIFICATION_REVIEW_INVALID"),
    ("model_mismatch", "QUALIFICATION_CONDITION_MISMATCH"),
    ("source_mismatch", "QUALIFICATION_CONDITION_MISMATCH"),
    ("legacy_timing", "QUALIFICATION_CONDITION_MISMATCH"),
    ("legacy_profile", "QUALIFICATION_CONDITION_MISMATCH"),
])
def test_invalid_evidence_blocks_both_entrypoints_before_any_preparation(
    authored_launch, monkeypatch, capsys, entrypoint, case, reason,
):
    config, report_path, review_path = authored_launch
    report = json.loads(report_path.read_text())
    review = json.loads(review_path.read_text())
    arguments = {"qualification_report": report_path, "qualification_review": review_path}
    if case == "missing_report":
        arguments = {}
    elif case == "missing_review":
        arguments.pop("qualification_review")
    elif case == "missing_report_file":
        report_path.unlink()
    elif case in {"failed", "running"}:
        report["status"] = case
    elif case == "incomplete":
        report["probes"].pop()
    elif case == "altered_report":
        report["authored_change_after_review"] = True
    elif case == "altered_trace":
        trace = report_path.parent / "private" / f"{report['probes'][0]['probe_id']}.json"
        trace.write_text('{"authored_changed_trace": true}')
    elif case == "rejected_review":
        review["approved"] = False
    elif case == "model_mismatch":
        config = config.model_copy(update={"llm_agent": "different-authored-model"})
    elif case == "source_mismatch":
        monkeypatch.setattr(runner, "_repository_revision", lambda: {
            "runtime_sources_sha256": "sha256:" + "b" * 64,
        })
    elif case == "legacy_timing":
        config = config.model_copy(update={"local_timing": None})
    elif case == "legacy_profile":
        report["profile"] = "authored_local_role_qualification_v1"
    if case != "missing_report_file":
        report_path.write_text(json.dumps(report))
    review_path.write_text(json.dumps(review))
    reached = []

    def forbidden(*args, **kwargs):
        reached.append(True)
        pytest.fail("Rejected evidence reached benchmark preparation or dispatch")

    monkeypatch.setattr(runner, "inspect_benchmark", forbidden)
    monkeypatch.setattr(preflight, "preflight_run", forbidden)
    monkeypatch.setattr(runner, "prepare_official_import", forbidden)
    monkeypatch.setattr(runner, "_load_runtime_factory", forbidden)
    monkeypatch.setattr(runner, "register_safe_agent", forbidden)
    if entrypoint == "runner":
        with pytest.raises(qualification.QualificationError, match=f"^{reason}$"):
            runner.run_official(config, tau_root=report_path.parent, **arguments)
    else:
        monkeypatch.setattr(cli, "load_config", lambda _: config)
        argv = ["run", "--config", "authored.toml"]
        for name, path in arguments.items():
            argv.extend(["--" + name.replace("_", "-"), str(path)])
        assert cli.main(argv) == 2
        assert json.loads(capsys.readouterr().out) == {"status": "blocked", "reason": reason}
    assert reached == []
    assert not Path(config.output_dir).exists()


@pytest.mark.parametrize("explicit_timing", [False, True])
def test_omitting_evidence_never_disables_the_local_safe_gate(tmp_path, monkeypatch, explicit_timing):
    config = local_config(tmp_path, **({} if explicit_timing else {"local_timing": None}))
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: pytest.fail("Inspection reached"))
    with pytest.raises(qualification.QualificationError, match="^QUALIFICATION_REPORT_REQUIRED$"):
        runner.run_official(config, tau_root=tmp_path)
    assert not Path(config.output_dir).exists()


@pytest.mark.parametrize("entrypoint", ["runner", "cli"])
def test_actual_files_are_validated_and_hashes_saved_before_official_import(
    authored_launch, monkeypatch, entrypoint,
):
    config, report_path, review_path = authored_launch
    expected = qualification.require_qualification(config, report_path, review_path)
    output = Path(config.output_dir)
    reached = []

    def inspect(*args):
        reached.append("inspect")
        return {"status": "ready", "selected_task_ids": ["authored_task"],
                "selected_tasks_with_nl_assertions": 0}

    def check_preflight(*args, **kwargs):
        reached.append("preflight")
        return {"ready_for_attempt": True, "authored_fixture": True}

    def stop_at_import(root):
        reached.append("import")
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["qualification_gate"] == expected
        assert manifest["launch_classification"] == "qualified_local_live"
        assert json.loads((output / "qualification-gate.json").read_text()) == expected
        assert "AUTHORED_PRIVATE_REVIEWER" not in (output / "manifest.json").read_text()
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "inspect_benchmark", inspect)
    monkeypatch.setattr(preflight, "preflight_run", check_preflight)
    monkeypatch.setattr(runner, "prepare_official_import", stop_at_import)
    with pytest.raises(KeyboardInterrupt):
        if entrypoint == "runner":
            runner.run_official(config, tau_root=report_path.parent,
                                qualification_report=report_path, qualification_review=review_path)
        else:
            monkeypatch.setattr(cli, "load_config", lambda _: config)
            cli.main(["run", "--config", "authored.toml", "--tau-root", str(report_path.parent),
                      "--qualification-report", str(report_path),
                      "--qualification-review", str(review_path)])
    assert reached == ["inspect", "preflight", "import"]
    assert json.loads((output / "manifest.json").read_text())["status"] == "interrupted"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "qualification-gate.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", ["injected", "custom", "stock", "hosted"])
def test_other_runtime_conditions_cannot_borrow_local_safe_qualification(
    authored_launch, monkeypatch, mode,
):
    config, report_path, review_path = authored_launch
    arguments = {}
    if mode == "injected":
        arguments["runtime_factory"] = lambda _: pytest.fail("Fixture runtime constructed")
    elif mode == "custom":
        config = config.model_copy(update={"runtime_factory": "authored.fixture:factory"})
    elif mode == "stock":
        config = config.model_copy(update={"system": "llm_agent"})
    else:
        config = BenchmarkConfig(output_dir=config.output_dir)
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: pytest.fail("Inspection reached"))
    with pytest.raises(qualification.QualificationError, match="^QUALIFICATION_SCOPE_UNSUPPORTED$"):
        runner.run_official(config, tau_root=report_path.parent,
                            qualification_report=report_path, qualification_review=review_path,
                            **arguments)
    assert not Path(config.output_dir).exists()


@pytest.mark.parametrize("mode", ["injected", "custom", "stock"])
def test_explicit_development_fixtures_and_stock_runs_keep_unqualified_labels(
    tmp_path, monkeypatch, mode,
):
    config = local_config(tmp_path)
    arguments = {}
    if mode == "injected":
        arguments["runtime_factory"] = lambda _: pytest.fail("Fixture runtime constructed")
    elif mode == "custom":
        config = config.model_copy(update={"runtime_factory": "authored.fixture:factory"})
    else:
        config = config.model_copy(update={"system": "llm_agent"})
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: {
        "status": "ready", "selected_task_ids": ["authored_task"],
        "selected_tasks_with_nl_assertions": 0,
    })
    monkeypatch.setattr(preflight, "preflight_run", lambda *args, **kwargs: {"ready_for_attempt": True})
    monkeypatch.setattr(qualification, "require_qualification",
                        lambda *args: pytest.fail("Fixture or stock incorrectly qualified"))

    def stop_at_import(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "prepare_official_import", stop_at_import)
    with pytest.raises(KeyboardInterrupt):
        runner.run_official(config, tau_root=tmp_path, **arguments)
    output = Path(config.output_dir)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["runtime_mode"] == ("stock_baseline" if mode == "stock" else f"{mode}_development")
    assert manifest["launch_classification"] == ("stock_baseline" if mode == "stock" else "development_fixture")
    assert "qualification_gate" not in manifest
    assert not (output / "qualification-gate.json").exists()
