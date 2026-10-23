"""Real loopback HTTP and pinned harness probes with authored, non-model replies."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from safe_agent.benchmarks.tau3 import preflight, runner
from safe_agent.benchmarks.tau3.config import BenchmarkConfig, harness_model_settings
from safe_agent.benchmarks.tau3.local_harness import harness_transport


@pytest.fixture
def local_server():
    state = {"requests": [], "status": 200, "models": ["authored-local:7b"]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(("GET", self.path, self.headers.get("Authorization"), None))
            payload = {"object": "list", "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "authored-fixture"}
                for model in state["models"]
            ]}
            self.respond(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("POST", self.path, self.headers.get("Authorization"), body))
            message = {"role": "assistant", "content": "Authored local fixture reply"}
            if body.get("tools"):
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "authored_call", "type": "function", "function": {
                        "name": "lookup", "arguments": '{"reference":"authored-reference"}',
                    },
                }]}
            self.respond({
                "id": "authored-completion", "object": "chat.completion", "created": 0,
                "model": body["model"], "choices": [{
                    "index": 0, "message": message,
                    "finish_reason": "tool_calls" if body.get("tools") else "stop",
                }], "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
            })

        def respond(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(state["status"])
            if state["status"] == 307:
                self.send_header("Location", "https://api.openai.com/v1/chat/completions")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def local_config(tmp_path, *, base="http://127.0.0.1:11434/v1", system="llm_agent"):
    return BenchmarkConfig(
        model_route="local", local_api_base=base, system=system,
        llm_agent="authored-local:7b", llm_user="authored-local:7b",
        development_task_ids=["authored_task"], output_dir=str(tmp_path / "run"),
    )


def launch_environment(monkeypatch, *, judge_tasks=0):
    inventory = {"status": "ready", "source_verified": True,
                 "selected_task_ids": ["authored_task"], "scheduled_conversations": 1,
                 "selected_tasks_with_nl_assertions": judge_tasks}
    monkeypatch.setattr(preflight, "inspect_benchmark", lambda *args: inventory)
    monkeypatch.setattr(preflight, "_dependencies", lambda: {"authored_test": True})
    monkeypatch.setattr(preflight, "_safe_revision", lambda: {"commit": "a" * 40, "dirty": False})
    monkeypatch.setattr(preflight, "_pinned_judge", lambda root: (
        "gpt-4.1-2025-04-14", {"temperature": 0.0}, 3,
    ))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "LITELLM_PROXY_API_BASE",
                 "LITELLM_API_BASE", "SAFE_EPISODE_SOCKET", "SAFE_EPISODE_DISPATCH_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    return inventory


def test_local_configuration_requires_explicit_endpoint_and_development_stage(tmp_path):
    for changes in ({"local_api_base": None}, {"stage": "coverage"},
                    {"stage": "final", "num_trials": 4}, {"model_route": "openai"}):
        values = local_config(tmp_path).model_dump() | changes
        with pytest.raises(ValueError):
            BenchmarkConfig.model_validate(values)
    hosted = BenchmarkConfig()
    assert hosted.model_route == "openai" and hosted.local_api_base is None
    assert harness_model_settings(hosted, "user") == (hosted.llm_user, hosted.llm_args_user)


@pytest.mark.parametrize("system", ["safe_agent", "llm_agent"])
def test_effective_official_settings_preserve_sidecar_boundary(monkeypatch, tmp_path, system):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("tau2")
    config = local_config(tmp_path, system=system)
    official = runner.text_run_config(config, agent_name="authored_agent", output_dir=tmp_path)
    assert official.llm_user == "openai/authored-local:7b"
    assert official.llm_args_user == {
        "temperature": 0.0, "api_base": config.local_api_base,
        "api_key": "safe-local-no-key", "num_retries": 0,
    }
    if system == "safe_agent":
        assert official.llm_agent == config.llm_agent
        assert official.llm_args_agent == {"temperature": 0.0}
    else:
        assert official.llm_agent == official.llm_user
        assert official.llm_args_agent == official.llm_args_user
    assert config.llm_args_user == {"temperature": 0.0}


def test_online_local_preflight_only_lists_models_and_needs_no_openai_key(
    monkeypatch, tmp_path, local_server,
):
    launch_environment(monkeypatch)
    config = local_config(tmp_path, base=local_server["base"])
    result = preflight.preflight_run(config, tau_root=tmp_path)
    assert result["ready_for_attempt"]
    assert result["harness_credential_presence"] == "not_required_for_local_route"
    assert result["model_calls"] == result["inference_requests"] == 0
    assert result["provider_requests"] == result["model_server_metadata_requests"] == 1
    assert result["local_server"]["verified"]
    assert not result["local_server"]["inference_verified"]
    assert result["model_roles"]["user"]["provider_num_retries"] == 0
    assert "judge" not in result["model_roles"]
    assert local_server["requests"] == [("GET", "/v1/models", "Bearer safe-local-no-key", None)]
    assert not Path(config.output_dir).exists()


def test_offline_local_preflight_never_contacts_endpoint(monkeypatch, tmp_path):
    launch_environment(monkeypatch)

    def no_probe(*args):
        raise AssertionError("Offline mode must not inspect model server")

    monkeypatch.setattr(preflight, "_probe_local_models", no_probe)
    result = preflight.preflight_run(local_config(tmp_path), tau_root=tmp_path, offline=True)
    assert result["configuration_validated"] and not result["ready_for_attempt"]
    assert result["provider_requests"] == result["model_server_metadata_requests"] == 0


@pytest.mark.parametrize("case", ["missing", "redirect", "unavailable"])
def test_local_model_listing_failure_blocks_readiness_without_inference(
    monkeypatch, tmp_path, local_server, case,
):
    launch_environment(monkeypatch)
    if case == "missing":
        local_server["models"] = []
    else:
        local_server["status"] = 307 if case == "redirect" else 503
    result = preflight.preflight_run(
        local_config(tmp_path, base=local_server["base"]), tau_root=tmp_path,
    )
    assert "LOCAL_MODEL_SERVER_UNAVAILABLE_OR_MODELS_MISSING" in result["issues"]
    assert not result["ready_for_attempt"]
    assert len(local_server["requests"]) == 1
    assert local_server["requests"][0][0] == "GET"


def test_local_tasks_requiring_hosted_judge_block_before_probe_or_run_artifacts(monkeypatch, tmp_path):
    inventory = launch_environment(monkeypatch, judge_tasks=1)
    probes = []
    monkeypatch.setattr(preflight, "_probe_local_models", lambda *args: probes.append(args))
    config = local_config(tmp_path)
    result = preflight.preflight_run(config, tau_root=tmp_path)
    assert "LOCAL_ROUTE_REQUIRES_DEVELOPMENT_TASKS_WITHOUT_HOSTED_JUDGE" in result["issues"]
    assert not probes and result["provider_requests"] == 0
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *args: inventory)
    with pytest.raises(ValueError, match="WITHOUT_HOSTED_JUDGE"):
        runner.run_official(config, tau_root=tmp_path, runtime_factory=lambda context: None)
    assert not Path(config.output_dir).exists()


@pytest.mark.parametrize("changes", [
    {"llm_agent": "hosted-model:cloud"}, {"llm_user": "model:120b-cloud"},
    {"llm_args_user": {"num_retries": 1}}, {"llm_args_user": {"num_retries": None}},
    {"llm_args_agent": {"api_base": "https://external.invalid"}},
])
def test_local_invalid_arguments_cannot_create_an_implicit_hosted_route(monkeypatch, tmp_path, changes):
    launch_environment(monkeypatch)
    config = local_config(tmp_path).model_copy(update=changes)
    result = preflight.preflight_run(config, tau_root=tmp_path, offline=True)
    assert not result["configuration_validated"]
    assert result["provider_requests"] == 0


def test_real_pinned_harness_sends_user_and_stock_tool_requests_only_to_local_server(
    monkeypatch, tmp_path, local_server,
):
    launch_environment(monkeypatch)
    pytest.importorskip("tau2")
    import litellm
    from tau2.data_model.message import UserMessage
    from tau2.environment.tool import Tool
    from tau2.utils.llm_utils import generate

    monkeypatch.setenv("OPENAI_API_KEY", "authored-hosted-key-must-not-be-forwarded")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    config = local_config(tmp_path, base=local_server["base"])
    previous_client = litellm.client_session
    previous_cache = litellm.in_memory_llm_clients_cache

    def lookup(reference: str) -> str:
        """Look up an authored reference."""
        raise AssertionError("Generation must never execute a proposed tool")

    for role in ("user", "agent"):
        # Reentering also checks that the SDK cannot reuse a closed local client.
        with harness_transport(config):
            assert litellm.client_session is not previous_client
            assert litellm.in_memory_llm_clients_cache is not previous_cache
            model, arguments = harness_model_settings(config, role)
            response = generate(
                model=model, messages=[UserMessage(role="user", content="Authored transport probe")],
                tools=[Tool(lookup)] if role == "agent" else None, **arguments,
            )
            if role == "agent":
                assert response.tool_calls[0].name == "lookup"
                assert response.tool_calls[0].arguments == {"reference": "authored-reference"}
            else:
                assert response.content == "Authored local fixture reply"
        assert litellm.client_session is previous_client
        assert litellm.in_memory_llm_clients_cache is previous_cache
    assert len(local_server["requests"]) == 2
    for method, path, authorization, body in local_server["requests"]:
        assert (method, path, authorization) == (
            "POST", "/v1/chat/completions", "Bearer safe-local-no-key",
        )
        assert body["model"] == "authored-local:7b"
        assert "authored-hosted-key" not in json.dumps(body)
    assert local_server["requests"][1][3]["tools"][0]["function"]["name"] == "lookup"


@pytest.mark.parametrize("status", [307, 503])
def test_real_pinned_local_harness_does_not_follow_redirects_or_retry(
    monkeypatch, tmp_path, local_server, status,
):
    launch_environment(monkeypatch)
    pytest.importorskip("tau2")
    from openai import APIError
    from tau2.data_model.message import UserMessage
    from tau2.utils.llm_utils import generate

    local_server["status"] = status
    config = local_config(tmp_path, base=local_server["base"])
    model, arguments = harness_model_settings(config, "user")
    with harness_transport(config), pytest.raises(APIError):
        generate(model=model, messages=[UserMessage(role="user", content="Authored failure")], **arguments)
    assert len(local_server["requests"]) == 1


def test_local_transport_refuses_undeclared_origin_and_restores_global_clients(monkeypatch, tmp_path):
    launch_environment(monkeypatch)
    pytest.importorskip("tau2")
    import litellm
    from tau2.utils import llm_utils  # noqa: F401

    previous = litellm.client_session
    with pytest.raises(ValueError, match="LOCAL_HARNESS_ROUTE_DENIED"), harness_transport(local_config(tmp_path)):
        litellm.client_session.post("https://api.openai.com/v1/chat/completions")
    assert litellm.client_session is previous
