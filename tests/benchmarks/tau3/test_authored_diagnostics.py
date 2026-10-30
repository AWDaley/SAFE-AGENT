"""Diagnostic lifecycle regressions with authored responses and zero inference."""

import importlib.util
import json
import sys
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.contracts.episode_inference import LocalTimingProfile


@pytest.fixture
def diagnostic_script(monkeypatch):
    path = Path(__file__).resolve().parents[3] / "scripts/run_tau3_authored_diagnostics.py"
    spec = importlib.util.spec_from_file_location("authored_diagnostic_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "preflight_run", lambda *args, **kwargs: {
        "ready_for_attempt": True, "authored_fixture": True,
    })
    monkeypatch.setattr(module, "prepare_official_import", lambda *args: None)
    monkeypatch.setattr(module, "qualification_condition", lambda config: {"authored_fixture": True})
    monkeypatch.setattr(module, "_repository_revision", lambda: {"authored_fixture": True})
    monkeypatch.setattr(module, "harness_transport", lambda config: nullcontext())
    monkeypatch.setattr(module, "agent_input", lambda probe: ([], "Authored fixture question"))
    disposed = []
    runtime = SimpleNamespace(episodes=SimpleNamespace(engine=SimpleNamespace(
        dispose=lambda: disposed.append(True),
    )))
    monkeypatch.setattr(module, "runtime_factory", lambda context: runtime)

    def no_inference(*args, **kwargs):
        pytest.fail("A diagnostic test attempted unconfigured inference")

    monkeypatch.setattr(module, "_run_agent_probe", no_inference)
    monkeypatch.setattr(module, "_run_user_probe", no_inference)
    return module, disposed


def local_config(tmp_path):
    return BenchmarkConfig(
        model_route="local", local_api_base="http://127.0.0.1:11434/v1",
        llm_agent="authored-local-model", llm_user="authored-local-model",
        output_dir=str(tmp_path / "benchmark"),
        local_timing=LocalTimingProfile(
            provider_timeout_seconds=90.0, transport_timeout_seconds=105.0,
            request_timeout_seconds=115.0,
        ),
    )


def probe_result(probe, *, statuses=("COMPLETE",), issues=(), tools=()):
    return {
        **asdict(probe), "passed": not issues, "issues": list(issues),
        "requests": [{"role": probe.role, "status": status, "seconds": 1.0,
                      "input_tokens": 100 if status == "COMPLETE" else None,
                      "output_tokens": 10 if status == "COMPLETE" else None}
                     for status in statuses],
        "tool_calls": list(tools), "successful_tools": list(tools),
        "trace_sha256": "a" * 64,
    }


@pytest.mark.parametrize("pack", [
    "agent-discovery", "customer-interface", "customer-native", "agent-latency",
])
def test_last_case_uncertainty_remains_stopped_and_cli_returns_nonzero(
    diagnostic_script, tmp_path, monkeypatch, capsys, pack,
):
    module, disposed = diagnostic_script
    observed = []

    def authored_probe(probe, *args, **kwargs):
        observed.append(probe.probe_id)
        last = len(observed) == len(module.PACKS[pack])
        return probe_result(probe, statuses=("UNCERTAIN",) if last else ("COMPLETE",))

    monkeypatch.setattr(module, "_run_agent_probe", authored_probe)
    monkeypatch.setattr(module, "_run_user_probe", authored_probe)
    monkeypatch.setattr(module, "load_config", lambda path: local_config(tmp_path))
    output = tmp_path / "diagnostic"
    monkeypatch.setattr(sys, "argv", [
        "run_tau3_authored_diagnostics.py", "--config", "authored.toml",
        "--tau-root", str(tmp_path / "frozen"), "--output-dir", str(output),
        "--pack", pack, "--max-seconds", "600",
    ])
    assert module.main() == 2
    report = json.loads((output / "report.json").read_text())
    assert len(observed) == report["recorded_cases"] == len(module.PACKS[pack])
    assert report["not_run_cases"] == 0
    assert report["status"] == "stopped"
    assert report["stop_reason"] == "DIAGNOSTIC_INFERENCE_INCOMPLETE"
    assert report["probes"][-1]["requests"][0]["status"] == "UNCERTAIN"
    assert report["eligible_for_qualification"] is False
    assert report["capability_score"] is None
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["status"] == "stopped"
    assert summary["stop_reason"] == "DIAGNOSTIC_INFERENCE_INCOMPLETE"
    assert disposed == ([True] if pack.startswith("agent-") else [])


def test_zero_requests_stops_before_the_next_case(diagnostic_script, tmp_path, monkeypatch):
    module, disposed = diagnostic_script
    observed = []

    def authored_probe(probe, *args, **kwargs):
        observed.append(probe.probe_id)
        return probe_result(probe, statuses=())

    monkeypatch.setattr(module, "_run_agent_probe", authored_probe)
    output = tmp_path / "diagnostic"
    report = module.run(local_config(tmp_path), output, tmp_path / "frozen", "agent-discovery", 600)
    assert len(observed) == report["recorded_cases"] == 1
    assert report["not_run_cases"] == len(module.PACKS["agent-discovery"]) - 1
    assert report["status"] == "stopped" and report["model_requests"] == 0
    assert report["stop_reason"] == "DIAGNOSTIC_INFERENCE_INCOMPLETE"
    assert "DIAGNOSTIC_INFERENCE_INCOMPLETE" in report["probes"][0]["issues"]
    assert json.loads((output / "report.json").read_text()) == report
    assert disposed == [True]


def test_late_probe_failure_retains_started_case_and_unknown_total(
    diagnostic_script, tmp_path, monkeypatch,
):
    module, disposed = diagnostic_script
    output = tmp_path / "diagnostic"

    def late_failure(probe, *args, **kwargs):
        checkpoint = json.loads((output / "report.json").read_text())
        assert checkpoint["started_cases"] == ["remember_fees"]
        raise OSError("Authored trace persistence failure after possible dispatch")

    monkeypatch.setattr(module, "_run_agent_probe", late_failure)
    report = module.run(local_config(tmp_path), output, tmp_path / "frozen", "agent-discovery", 600)
    assert report["status"] == "stopped" and report["recorded_cases"] == 0
    assert report["not_run_cases"] == len(module.PACKS["agent-discovery"]) - 1
    assert report["started_without_recorded_result"] == ["remember_fees"]
    assert report["request_accounting_complete"] is False
    assert report["model_requests"] is None and report["recorded_model_requests"] == 0
    assert disposed == [True]


@pytest.mark.parametrize("issue", ["AGENT_PROBE_EXECUTION_FAILED", "QUALIFICATION_INTERRUPTED"])
def test_custom_case_preserves_execution_failure_and_stops_after_completed_request(
    diagnostic_script, tmp_path, monkeypatch, issue,
):
    module, disposed = diagnostic_script
    cases = (
        module.Case("custom", "grounding", "An authored custom question"),
        module.Case("must_not_run", "grounding", "Another authored question"),
    )
    monkeypatch.setitem(module.PACKS, "agent-discovery", cases)
    observed = []

    def authored_probe(probe, *args, **kwargs):
        observed.append(probe.probe_id)
        return probe_result(probe, issues=(issue, "RETRIEVAL_ANSWER_NOT_GROUNDED"))

    monkeypatch.setattr(module, "_run_agent_probe", authored_probe)
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic",
                        tmp_path / "frozen", "agent-discovery", 600)
    assert observed == ["diagnostic_custom"]
    assert report["status"] == "stopped" and report["not_run_cases"] == 1
    assert report["stop_reason"] == "DIAGNOSTIC_EXECUTION_FAILED"
    result = report["probes"][0]
    assert result["requests"][0]["status"] == "COMPLETE"
    assert result["issues"] == [issue]
    assert result["reference_checker_issues"] == [issue, "RETRIEVAL_ANSWER_NOT_GROUNDED"]
    assert "passed" not in result and result["reference_checks_applicable"] is False
    assert disposed == [True]


def test_partial_failed_latency_probe_keeps_unnecessary_read_diagnostic(
    diagnostic_script, tmp_path, monkeypatch,
):
    module, disposed = diagnostic_script

    def partial_probe(probe, *args, **kwargs):
        return probe_result(probe, tools=("KB_search",), issues=("AGENT_PROBE_EXECUTION_FAILED",)) | {
            "trace_partial": True,
        }

    monkeypatch.setattr(module, "_run_agent_probe", partial_probe)
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic", tmp_path / "frozen",
                        "agent-latency", 600)
    result = report["probes"][0]
    assert report["status"] == "stopped" and report["stop_reason"] == "DIAGNOSTIC_EXECUTION_FAILED"
    assert report["model_requests"] == 1
    assert result["requests"][0]["status"] == "COMPLETE"
    assert result["tool_calls"] == result["successful_tools"] == ["KB_search"]
    assert result["trace_partial"] is True and result["passed"] is False
    assert result["issues"] == ["AGENT_PROBE_EXECUTION_FAILED", "DIAGNOSTIC_UNNECESSARY_READ"]
    assert report["eligible_for_qualification"] is False and report["capability_score"] is None
    assert disposed == [True]


def test_unavailable_partial_tool_summary_remains_null_when_a_read_was_required(
    diagnostic_script, tmp_path, monkeypatch,
):
    module, disposed = diagnostic_script
    monkeypatch.setitem(module.PACKS, "agent-latency", (
        module.Case("unknown_read", "grounding", requires_read=True),
    ))

    def unavailable_probe(probe, *args, **kwargs):
        return probe_result(probe, issues=("AGENT_PROBE_EXECUTION_FAILED",)) | {
            "tool_calls": None, "successful_tools": None, "trace_partial": True,
            "trace_messages_available": False,
        }

    monkeypatch.setattr(module, "_run_agent_probe", unavailable_probe)
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic", tmp_path / "frozen",
                        "agent-latency", 600)
    assert report["status"] == "stopped" and report["stop_reason"] == "DIAGNOSTIC_EXECUTION_FAILED"
    result = report["probes"][0]
    assert result["tool_calls"] is result["successful_tools"] is None
    assert result["trace_messages_available"] is False
    assert result["issues"] == ["AGENT_PROBE_EXECUTION_FAILED", "DIAGNOSTIC_REQUIRED_READ_MISSING"]
    assert report["model_requests"] == 1 and report["started_without_recorded_result"] == []
    assert disposed == [True]


@pytest.mark.parametrize(("tool", "missing"), [
    ("get_account_balance", True), ("KB_search", False),
    ("KB_search_bm25", False), ("grep", False),
])
def test_required_knowledge_read_cannot_be_satisfied_by_an_unrelated_successful_tool(
    diagnostic_script, tmp_path, monkeypatch, tool, missing,
):
    module, _ = diagnostic_script
    monkeypatch.setitem(module.PACKS, "agent-discovery", (
        module.Case("custom", "grounding", "Authored public policy question", requires_read=True),
    ))
    monkeypatch.setattr(module, "_run_agent_probe", lambda probe, *args, **kwargs: probe_result(
        probe, tools=(tool,),
    ))
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic",
                        tmp_path / "frozen", "agent-discovery", 600)
    result = report["probes"][0]
    assert ("DIAGNOSTIC_REQUIRED_READ_MISSING" in result["issues"]) is missing
    assert "passed" not in result
    assert report["eligible_for_qualification"] is False


@pytest.mark.parametrize("status", ["COMPLETE", "UNCERTAIN"])
@pytest.mark.parametrize("failure", [None, "start", "close"])
def test_optional_slot_observation_cannot_change_probe_outcome_or_request_accounting(
    diagnostic_script, tmp_path, monkeypatch, status, failure,
):
    module, disposed = diagnostic_script
    events = []

    class Observer:
        def __init__(self, base, path):
            assert base == "http://127.0.0.1:11434/v1"
            assert path == tmp_path / "diagnostic" / "local-slots.jsonl"

        def __enter__(self):
            events.append("enter")
            if failure == "start":
                raise OSError("Authored private observation setup failure")
            return self

        def __exit__(self, *args):
            events.append("exit")
            if failure == "close":
                raise OSError("Authored private observation cleanup failure")

        def summary(self):
            return {"status": "completed", "samples": 7, "diagnostic_only": True,
                    "eligible_for_qualification": False, "model_requests": 0}

    def user_probe(probe, *args):
        assert probe.role == "user" and probe.case == "native_tool"
        events.append("probe")
        return probe_result(probe, statuses=(status,))

    monkeypatch.setattr(module, "LocalLlamaObserver", Observer)
    monkeypatch.setattr(module, "_run_user_probe", user_probe)
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic", tmp_path / "frozen",
                        "customer-native", 600, observe_local_llama=True)
    assert events == ["enter", "probe", "exit"]
    assert disposed == []
    assert report["model_requests"] == report["recorded_model_requests"] == 1
    assert report["status"] == ("completed" if status == "COMPLETE" else "stopped")
    assert report["probes"][0]["requests"][0]["status"] == status
    assert report["eligible_for_qualification"] is False and report["capability_score"] is None
    assert report["local_observation"]["eligible_for_qualification"] is False
    if failure:
        assert report["local_observation"]["error_code"] == (
            "LOCAL_OBSERVATION_START_FAILED" if failure == "start"
            else "LOCAL_OBSERVATION_CLOSE_FAILED"
        )
        assert "Authored private" not in json.dumps(report)
    else:
        assert report["local_observation"]["samples"] == 7
    assert json.loads((tmp_path / "diagnostic/report.json").read_text()) == report


def test_default_diagnostic_does_not_contact_slot_endpoint(diagnostic_script, tmp_path, monkeypatch):
    module, _ = diagnostic_script

    def forbidden(*args, **kwargs):
        pytest.fail("Default diagnostics started slot observation")

    monkeypatch.setattr(module, "LocalLlamaObserver", forbidden)
    monkeypatch.setattr(module, "_run_user_probe", lambda probe, *args: probe_result(probe))
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic", tmp_path / "frozen",
                        "customer-native", 600)
    assert report["status"] == "completed" and "local_observation" not in report


def test_agent_latency_pack_reserves_only_one_model_attempt(diagnostic_script, tmp_path, monkeypatch):
    module, _ = diagnostic_script
    observed = []
    original_factory = module.runtime_factory

    def factory(context):
        observed.append(context)
        return original_factory(context)

    monkeypatch.setattr(module, "runtime_factory", factory)
    monkeypatch.setattr(module, "_run_agent_probe", lambda probe, *args, **kwargs: probe_result(probe))
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic", tmp_path / "frozen",
                        "agent-latency", 600)
    assert len(observed) == 1
    assert observed[0].episode_budget["max_model_calls"] == 1
    assert observed[0].run_budget["max_model_calls"] == 1
    assert report["effective_safe_budgets"]["episode_budget"]["max_model_calls"] == 1
    assert report["effective_safe_budgets"]["run_budget"]["max_model_calls"] == 1
    assert report["eligible_for_qualification"] is False


@pytest.mark.parametrize("reported_calls", [None, [], ["qualification_add"]])
def test_progress_does_not_turn_an_unreported_tool_summary_into_zero_calls(
    diagnostic_script, tmp_path, monkeypatch, capsys, reported_calls,
):
    module, _ = diagnostic_script

    def user_probe(probe, *args):
        result = probe_result(probe)
        if reported_calls is None:
            result.pop("tool_calls")
        else:
            result["tool_calls"] = reported_calls
        return result

    monkeypatch.setattr(module, "_run_user_probe", user_probe)
    report = module.run(local_config(tmp_path), tmp_path / "diagnostic", tmp_path / "frozen",
                        "customer-native", 600)
    progress = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    finished = next(row for row in progress if row["event"] == "case_finished")
    assert finished["tool_calls"] == reported_calls
    assert report["model_requests"] == 1 and report["status"] == "completed"
