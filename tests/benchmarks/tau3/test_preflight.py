"""Launch rejection and no-inference readiness with authored configuration."""

import json
from pathlib import Path

import pytest

from safe_agent.benchmarks.tau3 import preflight, runner
from safe_agent.benchmarks.tau3.config import BenchmarkConfig


@pytest.fixture
def launch(monkeypatch, tmp_path):
    inventory = {
        "status": "ready", "source_verified": True,
        "selected_task_ids": ["authored_task"], "scheduled_conversations": 1,
        "selected_tasks_with_nl_assertions": 1,
    }
    monkeypatch.setattr(preflight, "inspect_benchmark", lambda *args: inventory)
    monkeypatch.setattr(preflight, "_dependencies", lambda: {"authored_test": True})
    monkeypatch.setattr(preflight, "_safe_revision", lambda: {"commit": "a" * 40, "dirty": False})
    monkeypatch.setattr(preflight, "_pinned_judge", lambda root: (
        "gpt-4.1-2025-04-14", {"temperature": 0.0}, 3,
    ))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    for name in ("OPENAI_API_KEY", "SAFE_EPISODE_SOCKET", "SAFE_EPISODE_DISPATCH_KEY_FILE",
                 "OPENAI_BASE_URL", "OPENAI_API_BASE", "LITELLM_PROXY_API_BASE", "LITELLM_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    config = BenchmarkConfig(output_dir=str(tmp_path / "new-run"),
                             development_task_ids=["authored_task"])
    return config, tmp_path, inventory


def configured_process(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "authored-harness-credential-canary")
    monkeypatch.setenv("SAFE_EPISODE_SOCKET", "/authored/private/episode.sock")
    monkeypatch.setenv("SAFE_EPISODE_DISPATCH_KEY_FILE", "/authored/private/dispatch-key")


def test_offline_preflight_never_contacts_service_or_requires_credentials(launch, monkeypatch):
    config, root, _ = launch

    def no_probe(*args):
        raise AssertionError("Offline preflight must not read keys or probe the service")

    monkeypatch.setattr(preflight, "_probe_service", no_probe)
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert result["status"] == "configuration_validated"
    assert result["configuration_validated"]
    assert not result["ready_for_attempt"]
    assert result["harness_credential_presence"] == "not_checked"
    assert result["model_calls"] == result["provider_requests"] == 0
    assert not Path(config.output_dir).exists()


def test_online_preflight_requires_credentials_and_route_without_creating_artifacts(launch):
    config, root, _ = launch
    result = preflight.preflight_run(config, tau_root=root)
    assert result["status"] == "blocked"
    assert result["configuration_validated"]
    assert set(result["issues"]) == {
        "HARNESS_OPENAI_CREDENTIAL_NOT_CONFIGURED",
        "SIDECAR_SOCKET_AND_DISPATCH_KEY_NOT_CONFIGURED",
    }
    assert not Path(config.output_dir).exists()


def test_matching_capability_is_launch_readiness_not_provider_access(launch, monkeypatch):
    config, root, _ = launch
    configured_process(monkeypatch)
    probes = []

    def probe(value):
        probes.append(value.llm_agent)
        return {"verified": True, "provider_access_verified": False, "authored_proof": True}

    monkeypatch.setattr(preflight, "_probe_service", probe)
    result = preflight.preflight_run(config, tau_root=root)
    assert probes == [config.llm_agent]
    assert result["ready_for_attempt"] and result["status"] == "ready_for_attempt"
    assert not result["provider_access_verified"]
    assert result["model_roles"]["agent"]["provider_num_retries"] == 0
    assert result["model_roles"]["user"]["provider_num_retries"] == 3
    assert result["model_roles"]["judge"]["provider_num_retries"] == 3
    assert result["simulation_max_retries"] == 0
    assert "authored-harness-credential-canary" not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {"episode_budget": {"unsupported_budget": 1}},
    {"run_budget": {"max_turns": 0}},
    {"llm_args_agent": {"api_key": "must-not-appear-in-output"}},
    {"llm_args_agent": {"temperature": True}},
    {"llm_args_agent": {"seed": True}},
    {"llm_args_user": {"api_base": "https://private.invalid/must-not-appear-in-output"}},
    {"llm_args_user": {"num_retries": -1}},
])
def test_invalid_runtime_configuration_is_rejected_before_service_probe(launch, monkeypatch, changes):
    config, root, _ = launch
    configured_process(monkeypatch)
    calls = []
    monkeypatch.setattr(preflight, "_probe_service", lambda value: calls.append(value))
    result = preflight.preflight_run(config.model_copy(update=changes), tau_root=root)
    assert "RUNTIME_LIMITS_OR_MODEL_ARGUMENTS_INVALID" in result["issues"]
    assert result["status"] == "blocked" and not calls
    assert "must-not-appear-in-output" not in json.dumps(result)
    assert not Path(config.output_dir).exists()


def test_too_little_reserved_run_headroom_is_detected(launch):
    config, root, inventory = launch
    inventory["scheduled_conversations"] = 5
    config = config.model_copy(update={
        "episode_budget": {"max_model_calls": 4}, "run_budget": {"max_model_calls": 4},
    })
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert "RUN_CANNOT_RESERVE_ONE_ATTEMPT_PER_CONVERSATION" in result["issues"]


def test_run_must_allow_the_initial_customer_turn_for_each_conversation(launch):
    config, root, inventory = launch
    inventory["scheduled_conversations"] = 5
    config = config.model_copy(update={
        "episode_budget": {"max_turns": 1}, "run_budget": {"max_turns": 1},
    })
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert "RUN_CANNOT_RESERVE_ONE_ATTEMPT_PER_CONVERSATION" in result["issues"]


@pytest.mark.parametrize("cap", [
    "max_turns", "max_model_calls", "max_tool_calls", "max_tokens", "max_cost_microusd",
])
def test_episode_caps_cannot_exceed_the_containing_run(launch, cap):
    config, root, _ = launch
    config = config.model_copy(update={
        "episode_budget": {cap: 100_000}, "run_budget": {cap: 99_999},
    })
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert "RUNTIME_LIMITS_OR_MODEL_ARGUMENTS_INVALID" in result["issues"]
    assert not result["configuration_validated"]
    assert not Path(config.output_dir).exists()


@pytest.mark.parametrize("role", ["user", "agent"])
def test_rejected_retry_argument_does_not_echo_private_value(launch, role):
    config, root, _ = launch
    config = config.model_copy(update={
        "system": "llm_agent", f"llm_args_{role}": {
            "num_retries": "authored-private-invalid-retry-value",
        },
    })
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert "RUNTIME_LIMITS_OR_MODEL_ARGUMENTS_INVALID" in result["issues"]
    assert result["model_roles"][role]["provider_num_retries"] is None
    assert "authored-private-invalid-retry-value" not in json.dumps(result)


def test_nonfinite_arguments_fail_with_structured_output(launch):
    config, root, _ = launch
    config = config.model_copy(update={"llm_args_user": {"temperature": float("nan")}})
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert result["issues"] == ["CONFIGURATION_MUST_BE_FINITE_JSON"]
    assert result["config_sha256"] is None
    json.dumps(result, allow_nan=False)


def test_token_headroom_includes_the_mandatory_request_envelope(launch):
    config, root, _ = launch
    config = config.model_copy(update={"episode_budget": {"max_tokens": 8192 + 2048}})
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert "EPISODE_CANNOT_RESERVE_A_MODEL_ATTEMPT" in result["issues"]
    assert result["effective_safe_limits"]["minimum_token_hold"] > 8192 + 2048
    config = config.model_copy(update={
        "episode_budget": {"max_tokens": 8192 + 2048},
        "run_budget": {"max_tokens": 8192 + 2048},
    })
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert "RUN_CANNOT_RESERVE_ONE_ATTEMPT_PER_CONVERSATION" in result["issues"]


def test_null_harness_retry_setting_uses_the_pinned_default(launch):
    config, root, _ = launch
    config = config.model_copy(update={"llm_args_user": {"num_retries": None}})
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert result["model_roles"]["user"]["provider_num_retries"] == 3


def test_final_dirty_revision_blocks_before_artifact_creation(launch, monkeypatch):
    config, root, _ = launch
    monkeypatch.setattr(preflight, "_safe_revision", lambda: {"commit": "a" * 40, "dirty": True})
    config = config.model_copy(update={"stage": "final", "num_trials": 4})
    result = preflight.preflight_run(config, tau_root=root, offline=True)
    assert "FINAL_RUN_REQUIRES_CLEAN_RECORDED_REVISION" in result["issues"]
    assert not Path(config.output_dir).exists()


def test_development_tasks_require_declared_exposure(launch):
    config, root, _ = launch
    result = preflight.preflight_run(
        config.model_copy(update={"development_task_ids": []}), tau_root=root, offline=True,
    )
    assert "PILOT_SELECTION_MUST_DECLARE_DEVELOPMENT_EXPOSURE" in result["issues"]


def test_nonempty_or_symlink_output_is_rejected_without_changes(launch, tmp_path):
    config, root, _ = launch
    output = Path(config.output_dir)
    output.mkdir()
    canary = output / "keep.txt"
    canary.write_text("existing material")
    assert "OUTPUT_DIRECTORY_UNAVAILABLE" in preflight.preflight_run(
        config, tau_root=root, offline=True,
    )["issues"]
    link = tmp_path / "linked-output"
    link.symlink_to(output, target_is_directory=True)
    assert "OUTPUT_DIRECTORY_UNAVAILABLE" in preflight.preflight_run(
        config.model_copy(update={"output_dir": str(link)}), tau_root=root, offline=True,
    )["issues"]
    assert canary.read_text() == "existing material"


def test_failed_capability_verification_does_not_emit_private_error(launch, monkeypatch):
    config, root, _ = launch
    configured_process(monkeypatch)

    def fail(value):
        raise RuntimeError("private-key-or-provider-body-canary")

    monkeypatch.setattr(preflight, "_probe_service", fail)
    result = preflight.preflight_run(config, tau_root=root)
    assert "SIDECAR_CAPABILITY_VERIFICATION_FAILED" in result["issues"]
    assert "private-key-or-provider-body-canary" not in json.dumps(result)


def test_stock_checks_do_not_require_safe_service_or_apply_safe_budgets(launch, monkeypatch):
    config, root, inventory = launch
    inventory["selected_tasks_with_nl_assertions"] = 0
    monkeypatch.setenv("OPENAI_API_KEY", "authored-harness-key")
    config = config.model_copy(update={"system": "llm_agent", "episode_budget": {"ignored": True}})
    result = preflight.preflight_run(config, tau_root=root)
    assert result["ready_for_attempt"]
    assert result["sidecar"]["status"] == "not_used_by_stock_agent"
    assert result["model_roles"]["agent"]["provider_num_retries"] == 3
    assert "judge" not in result["model_roles"]
    assert not result["safe_budget_applies"]


def test_implicit_environment_and_endpoint_override_are_explicit_launch_blockers(launch, monkeypatch):
    config, root, _ = launch
    configured_process(monkeypatch)
    monkeypatch.setattr(preflight, "_probe_service", lambda value: {"verified": True})
    monkeypatch.setenv("LITELLM_MODE", "DEV")
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://private-endpoint-canary.invalid")
    result = preflight.preflight_run(config, tau_root=root)
    assert {"SET_LITELLM_MODE_PRODUCTION_TO_DISABLE_IMPLICIT_DOTENV",
            "SET_LITELLM_LOCAL_MODEL_COST_MAP_TRUE",
            "PROVIDER_ENDPOINT_OVERRIDE_UNSUPPORTED"}.issubset(result["issues"])
    assert "private-endpoint-canary" not in json.dumps(result)


def test_pinned_judge_constants_are_read_without_executing_source(tmp_path):
    config_file = tmp_path / "src/tau2/config.py"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        "raise AssertionError('must never execute source')\n"
        "DEFAULT_LLM_NL_ASSERTIONS = 'gpt-authored-judge'\n"
        "DEFAULT_LLM_NL_ASSERTIONS_TEMPERATURE = 0.0\n"
        "DEFAULT_LLM_NL_ASSERTIONS_ARGS = {'temperature': DEFAULT_LLM_NL_ASSERTIONS_TEMPERATURE}\n"
        "DEFAULT_MAX_RETRIES = 3\n"
    )
    assert preflight._pinned_judge(tmp_path) == ("gpt-authored-judge", {"temperature": 0.0}, 3)


def test_runner_preflight_rejection_precedes_artifacts_and_official_import(launch, monkeypatch):
    config, root, inventory = launch
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: inventory)
    report = {"ready_for_attempt": False, "status": "blocked", "issues": ["AUTHORED_BLOCKER"]}
    monkeypatch.setattr(preflight, "preflight_run", lambda *args, **kwargs: report)

    def forbidden(*args):
        raise AssertionError("Blocked preflight must not import the official runner")

    monkeypatch.setattr(runner, "prepare_official_import", forbidden)
    with pytest.raises(preflight.PreflightError) as caught:
        runner.run_official(config, tau_root=root)
    assert caught.value.report is report
    assert not Path(config.output_dir).exists()


def test_cli_offline_preflight_and_blocked_launch_are_structured(launch, monkeypatch, capsys):
    from safe_agent.benchmarks.tau3.__main__ import main

    config, root, _ = launch
    monkeypatch.setattr("safe_agent.benchmarks.tau3.__main__.load_config", lambda path: config)
    assert main(["preflight", "--config", "authored.toml", "--tau-root", str(root), "--offline"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_validated"
    assert main(["preflight", "--config", "authored.toml", "--tau-root", str(root)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


def test_cli_preflight_report_cannot_occupy_the_run_directory(launch, monkeypatch, capsys):
    from safe_agent.benchmarks.tau3.__main__ import main

    config, root, _ = launch
    monkeypatch.setattr("safe_agent.benchmarks.tau3.__main__.load_config", lambda path: config)
    output = str(Path(config.output_dir) / "preflight.json")
    assert main(["preflight", "--config", "authored.toml", "--tau-root", str(root),
                 "--offline", "--output", output]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "INVALID_PREFLIGHT_OUTPUT"
    assert not Path(config.output_dir).exists()
