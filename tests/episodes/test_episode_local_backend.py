"""Loopback compatibility with authored HTTP responses and no model inference."""

from __future__ import annotations

import builtins
import copy
import json
import threading
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from pydantic import SecretStr

from safe_agent.contracts.episode_inference import (
    EpisodeBackendCapabilities,
    EpisodeBackendRoute,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    EpisodeServiceCapabilities,
    validate_local_api_base,
    wire_digest,
)
from safe_agent.inference.episode_gateway import EpisodeInferenceDenied, EpisodeUnixTransport
from safe_agent.inference_service import episode_service
from safe_agent.inference_service.episode_service import DurableServiceFence, create_episode_service
from safe_agent.inference_service.providers.local_openai_episode import (
    MAX_PROVIDER_RESPONSE_BYTES,
    LocalOpenAIEpisodeBackend,
    local_openai_backend_capabilities,
)
from safe_agent.inference_service.providers.openai_episode import openai_backend_capabilities
from tests.episodes.test_episode_capabilities import (
    DISPATCH_KEY,
    PROVIDER_CANARY,
    authenticator,
    running_sidecar,
)
from tests.episodes.test_episode_foundation import NOW
from tests.episodes.test_episode_model_boundary import InProcessClient

MODEL = "authored-local:tiny"
BASE = "http://127.0.0.1:11434/v1"
ANSWER = '{"kind":"message","purpose":"answer","text":"Authored fixture response."}'


def request(**changes):
    return EpisodeInferenceRequest(**{
        "request_id": "local_fixture", "episode_id": "ep_local_fixture", "domain": "retail",
        "model": MODEL, "policy_hash": wire_digest("policy"),
        "observation_hash": wire_digest("observation"),
        "projected_context": {"message": "Use only [[safe:user_id:fixture]]"},
        "max_output_tokens": 64, "temperature": 0.0, "seed": 42,
        "issued_at": NOW, "deadline": NOW + timedelta(seconds=60), **changes,
    })


def completion():
    return {
        "id": "authored_completion", "object": "chat.completion", "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


@contextmanager
def mock_backend(monkeypatch, handler, *, model=MODEL, base_url=BASE):
    options = []

    def transport(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(handler)

    with monkeypatch.context() as constructor_patch:
        constructor_patch.setattr(httpx, "HTTPTransport", transport)
        backend = LocalOpenAIEpisodeBackend(model=model, base_url=base_url)
    try:
        yield backend, options
    finally:
        backend.close()


@pytest.mark.parametrize("port", [1, 11434, 65535])
def test_local_base_accepts_only_a_canonical_numeric_loopback_origin(port):
    origin = f"http://127.0.0.1:{port}/v1"
    assert validate_local_api_base(origin) == origin
    assert EpisodeBackendRoute(kind="local_openai", local_api_base=origin).local_api_base == origin


@pytest.mark.parametrize("origin", [
    "https://127.0.0.1:11434/v1", "http://localhost:11434/v1", "http://[::1]:11434/v1",
    "http://127.0.0.2:11434/v1", "http://127.1:11434/v1", "http://2130706433:11434/v1",
    "http://127.0.0.1/v1", "http://127.0.0.1:0/v1", "http://127.0.0.1:65536/v1",
    "http://127.0.0.1:011434/v1", "http://127.0.0.1:11434/v1/",
    "http://127.0.0.1:11434/v1?api_key=secret", "http://127.0.0.1:11434/v1#fragment",
    "http://secret@127.0.0.1:11434/v1", "http://127.0.0.1:11434@remote.invalid/v1",
    "http://127.0.0.1:11434/other/../v1", "http://127.0.0.1:11434/%76%31",
    " http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1\n",
    "http://127.0.0.1:11434\\@remote.invalid/v1", "http://192.168.1.1:11434/v1", 11434,
])
def test_local_base_rejects_url_reinterpretation_and_nonloopback_routes(origin):
    with pytest.raises(ValueError, match="LOCAL_MODEL_API_BASE_DENIED"):
        validate_local_api_base(origin)


@pytest.mark.parametrize("values", [
    {"kind": "local_openai"}, {"kind": "hosted_openai", "local_api_base": BASE},
    {"kind": "remote_compatible", "local_api_base": BASE},
])
def test_typed_backend_selection_rejects_ambiguous_routes(values):
    with pytest.raises(ValueError):
        EpisodeBackendRoute.model_validate(values)


def test_capability_factory_is_pure_and_profiles_cannot_be_relabelled(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("Pure descriptor constructed HTTP client"))
    local = local_openai_backend_capabilities(model=MODEL, base_url=BASE)
    hosted = openai_backend_capabilities(model=MODEL)
    assert local.profile == "local_openai_chat_v1" and local.provider_origin == BASE
    assert local.model == hosted.model and local.profile != hosted.profile
    assert local.candidate_schema_hash == hosted.candidate_schema_hash
    assert local.system_instruction_hash == hosted.system_instruction_hash
    assert local.provider_retries == 0 and not local.follow_redirects and not local.trust_env
    for value in (
        {**local.model_dump(), "profile": "openai_chat_v1"},
        {**hosted.model_dump(), "profile": "local_openai_chat_v1"},
        {**local.model_dump(), "provider_origin": "https://remote.invalid/v1"},
        {**local.model_dump(), "profile": "scripted_fixture_v1"},
    ):
        with pytest.raises(ValueError):
            EpisodeBackendCapabilities.model_validate(value)


def test_local_wire_uses_max_tokens_and_never_forwards_hosted_credentials(monkeypatch):
    observed = []
    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_CANARY)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://remote.invalid/v1")
    monkeypatch.setenv("HTTP_PROXY", "http://remote.invalid:8080")

    def handler(inbound):
        observed.append(inbound)
        return httpx.Response(200, json=completion())

    with mock_backend(monkeypatch, handler) as (backend, options):
        result = backend.infer(request())
        assert backend.capabilities == local_openai_backend_capabilities(model=MODEL, base_url=BASE)
    assert options == [{"retries": 0, "trust_env": False}]
    assert len(observed) == 1
    sent = observed[0]
    assert sent.method == "POST" and str(sent.url) == BASE + "/chat/completions"
    assert "authorization" not in sent.headers and "proxy-authorization" not in sent.headers
    assert sent.headers["accept-encoding"] == "identity"
    assert set(sent.extensions["timeout"].values()) == {60.0}
    payload = json.loads(sent.content)
    assert payload["model"] == MODEL and payload["max_tokens"] == 64
    assert payload["seed"] == 42 and payload["temperature"] == 0.0
    assert payload["stream"] is False and payload["response_format"] == {"type": "json_object"}
    assert "max_completion_tokens" not in payload
    assert PROVIDER_CANARY not in sent.content.decode() and DISPATCH_KEY not in sent.content.decode()
    assert result.output_json == ANSWER and result.usage_source == "provider_reported"
    assert result.input_tokens == 12 and result.output_tokens == 8
    assert result.request_id == request().request_id and result.input_hash == request().digest


@pytest.mark.parametrize("value", [64.0, True, "64"])
def test_request_output_limit_remains_strict_and_noncoercing(value):
    with pytest.raises(ValueError):
        request(max_output_tokens=value)


@pytest.mark.parametrize("case", [
    "wrong_model", "missing_usage", "prompt_string", "prompt_bool", "negative_prompt",
    "completion_float", "completion_bool", "completion_over_limit", "negative_completion",
    "invalid_total", "total_string", "multiple_choices", "length_stop", "nonassistant",
    "tool_calls", "refusal", "empty_content", "list_content", "duplicate_key", "nonfinite",
])
def test_invalid_response_or_usage_is_uncertain_without_retry(monkeypatch, case):
    response = completion()
    message = response["choices"][0]["message"]
    if case == "wrong_model":
        response["model"] = "wrong_local_model"
    elif case == "missing_usage":
        response.pop("usage")
    elif case in {"prompt_string", "prompt_bool", "negative_prompt"}:
        response["usage"]["prompt_tokens"] = {"prompt_string": "12", "prompt_bool": True,
                                               "negative_prompt": -1}[case]
    elif case in {"completion_float", "completion_bool", "completion_over_limit", "negative_completion"}:
        response["usage"]["completion_tokens"] = {
            "completion_float": 8.0, "completion_bool": False,
            "completion_over_limit": 65, "negative_completion": -1,
        }[case]
    elif case in {"invalid_total", "total_string"}:
        response["usage"]["total_tokens"] = 21 if case == "invalid_total" else "20"
    elif case == "multiple_choices":
        response["choices"].append(copy.deepcopy(response["choices"][0]))
    elif case == "length_stop":
        response["choices"][0]["finish_reason"] = "length"
    elif case == "nonassistant":
        message["role"] = "user"
    elif case == "tool_calls":
        message["tool_calls"] = [{"id": "not_a_candidate"}]
    elif case == "refusal":
        message["refusal"] = "Authored refusal"
    elif case == "empty_content":
        message["content"] = ""
    elif case == "list_content":
        message["content"] = ["Authored content"]
    raw = json.dumps(response)
    if case == "duplicate_key":
        raw = '{"model":"duplicate",' + raw[1:]
    elif case == "nonfinite":
        raw = raw.replace('"prompt_tokens": 12', '"prompt_tokens": NaN')
    observed = []

    def handler(inbound):
        observed.append(inbound)
        return httpx.Response(200, content=raw, headers={"Content-Type": "application/json"})

    with mock_backend(monkeypatch, handler) as (backend, _):
        with pytest.raises(ValueError, match="^LOCAL_MODEL_RESPONSE_UNCERTAIN$") as failure:
            backend.infer(request())
    assert len(observed) == 1 and failure.value.__cause__ is None


@pytest.mark.parametrize("status,headers", [
    (301, {"Location": "https://remote.invalid/v1/chat/completions"}),
    (307, {"Location": "http://127.0.0.1:1/v1/chat/completions"}),
    (500, {}), (200, {"Content-Type": "text/plain"}),
    (200, {"Content-Encoding": "custom-compression"}),
])
def test_redirect_error_and_unexpected_encoding_never_fall_back(monkeypatch, status, headers):
    observed = []

    def handler(inbound):
        observed.append(inbound)
        return httpx.Response(status, content=b"{}", headers={"Content-Type": "application/json", **headers})

    with mock_backend(monkeypatch, handler) as (backend, _):
        with pytest.raises(ValueError, match="LOCAL_MODEL_RESPONSE_UNCERTAIN"):
            backend.infer(request())
    assert len(observed) == 1


def test_response_stream_stops_at_byte_limit_without_reading_remainder(monkeypatch):
    chunks = []

    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            chunks.append(1)
            yield b" " * MAX_PROVIDER_RESPONSE_BYTES
            chunks.append(2)
            yield b" "
            pytest.fail("Response continued after exceeding its bound")

    with mock_backend(monkeypatch, lambda _: httpx.Response(
        200, stream=Oversized(), headers={"Content-Type": "application/json"},
    )) as (backend, _):
        with pytest.raises(ValueError, match="LOCAL_MODEL_RESPONSE_UNCERTAIN"):
            backend.infer(request())
    assert chunks == [1, 2]


def test_connection_failure_is_sanitized_and_not_retried(monkeypatch):
    observed = []

    def fail(inbound):
        observed.append(inbound)
        raise httpx.ConnectError(PROVIDER_CANARY)

    with mock_backend(monkeypatch, fail) as (backend, _):
        with pytest.raises(ValueError, match="^LOCAL_MODEL_RESPONSE_UNCERTAIN$") as failure:
            backend.infer(request())
    assert len(observed) == 1 and failure.value.__cause__ is None
    assert PROVIDER_CANARY not in str(failure.value)


def test_wrong_model_never_opens_the_local_route(monkeypatch):
    with mock_backend(monkeypatch, lambda _: pytest.fail("Wrong model reached transport")) as (backend, _):
        with pytest.raises(ValueError, match="MODEL_ROUTE_DENIED"):
            backend.infer(request(model="different_model"))


def test_local_selection_never_reads_a_provider_key_or_imports_openai(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "openai" or name.startswith("openai."):
            pytest.fail("Local route attempted to import the hosted SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setenv("SAFE_EPISODE_BACKEND", "local_openai")
    monkeypatch.setenv("SAFE_EPISODE_LOCAL_BASE_URL", BASE)
    monkeypatch.setenv("SAFE_EPISODE_OPENAI_KEY_FILE", "/never-read-provider-key")
    monkeypatch.setattr(episode_service, "_secret_from_file", lambda _: pytest.fail("Local route read a hosted key"))
    backend = episode_service.backend_from_environment(model=MODEL, dispatch_key=SecretStr(DISPATCH_KEY))
    try:
        assert isinstance(backend, LocalOpenAIEpisodeBackend)
        assert backend.capabilities == local_openai_backend_capabilities(model=MODEL, base_url=BASE)
    finally:
        backend.close()


def test_default_hosted_selection_retains_fixed_provider_and_distinct_key(monkeypatch):
    from safe_agent.inference_service.providers import openai_episode

    for name in ("SAFE_EPISODE_BACKEND", "SAFE_EPISODE_LOCAL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAFE_EPISODE_OPENAI_KEY_FILE", "/authored-key-file")
    monkeypatch.setattr(episode_service, "_secret_from_file", lambda _: SecretStr(PROVIDER_CANARY))
    observed = []
    monkeypatch.setattr(openai_episode, "OpenAIEpisodeBackend", lambda **kwargs: observed.append(kwargs))
    episode_service.backend_from_environment(model=MODEL, dispatch_key=SecretStr(DISPATCH_KEY))
    assert observed == [{"model": MODEL, "api_key": SecretStr(PROVIDER_CANARY)}]
    with pytest.raises(ValueError, match="SEPARATE_PROVIDER_AND_DISPATCH_KEYS_REQUIRED"):
        episode_service.backend_from_environment(model=MODEL, dispatch_key=SecretStr(PROVIDER_CANARY))
    assert len(observed) == 1


@contextmanager
def authored_loopback_server(*, status=200):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            observed.append((self.path, dict(self.headers), json.loads(body)))
            reply = json.dumps(completion()).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            if status == 307:
                self.send_header("Location", "http://127.0.0.1:1/v1/chat/completions")
            self.end_headers()
            self.wfile.write(reply)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("status", [200, 307])
def test_real_loopback_http_ignores_proxy_env_and_refuses_redirect(monkeypatch, status):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with authored_loopback_server(status=status) as (base_url, observed):
        backend = LocalOpenAIEpisodeBackend(model=MODEL, base_url=base_url, timeout_seconds=2.0)
        try:
            if status == 200:
                assert backend.infer(request()).output_json == ANSWER
            else:
                with pytest.raises(ValueError, match="LOCAL_MODEL_RESPONSE_UNCERTAIN"):
                    backend.infer(request())
        finally:
            backend.close()
    assert len(observed) == 1
    path, headers, payload = observed[0]
    assert path == "/v1/chat/completions" and payload["max_tokens"] == 64
    assert all(key.lower() != "authorization" for key in headers)


def test_real_uds_capabilities_bind_local_origin_without_querying_model(tmp_path, monkeypatch):
    with mock_backend(monkeypatch, lambda _: pytest.fail("Capabilities queried a model")) as (backend, _):
        fence = DurableServiceFence(tmp_path / "local-capabilities.sqlite")
        original = fence.path.read_bytes()
        app = create_episode_service(model=MODEL, authenticator=authenticator(), backend=backend,
                                     fence=fence, clock=lambda: NOW)
        expected = EpisodeServiceCapabilities(backend=backend.capabilities)
        with running_sidecar(app, tmp_path / "l.sock") as path:
            transport = EpisodeUnixTransport(path, timeout_seconds=2)
            proof = transport.capabilities(authenticator(), expected=expected, clock=lambda: NOW)
            assert proof.reply.capabilities == expected
            assert not proof.reply.capabilities.provider_access_verified
            for wrong in (
                openai_backend_capabilities(model=MODEL),
                local_openai_backend_capabilities(model=MODEL, base_url="http://127.0.0.1:8080/v1"),
            ):
                with pytest.raises(EpisodeInferenceDenied, match="CAPABILITIES_VERIFICATION_FAILED"):
                    transport.capabilities(authenticator(), expected=EpisodeServiceCapabilities(backend=wrong),
                                           clock=lambda: NOW)
        assert fence.path.read_bytes() == original


def test_authorized_local_dispatch_is_executed_once_and_uses_existing_fence(tmp_path, monkeypatch):
    observed = []

    def handler(inbound):
        observed.append(inbound)
        return httpx.Response(200, json=completion())

    with mock_backend(monkeypatch, handler) as (backend, _):
        app = create_episode_service(model=MODEL, authenticator=authenticator(), backend=backend,
                                     fence=DurableServiceFence(tmp_path / "once.sqlite"), clock=lambda: NOW)
        signed = authenticator().sign(request(), wire_digest("authored reservation"))
        with InProcessClient(app, base_url="http://safe-episode") as client:
            first = client.post("/v1/episode/infer", content=signed.model_dump_json(),
                                headers={"Content-Type": "application/json"})
            second = client.post("/v1/episode/infer", content=signed.model_dump_json(),
                                 headers={"Content-Type": "application/json"})
        assert first.status_code == 200 and second.status_code == 409
        assert EpisodeInferenceReply.model_validate_json(first.content).output_json == ANSWER
    assert len(observed) == 1
