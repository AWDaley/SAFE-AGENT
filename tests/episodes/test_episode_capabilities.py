"""Authenticated readiness over the real UDS without any model invocation."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from safe_agent.contracts.episode_inference import (
    CANDIDATE_ADAPTER,
    EpisodeBackendCapabilities,
    EpisodeCapabilitiesReply,
    EpisodeCapabilitiesReplyEnvelope,
    EpisodeCapabilitiesRequest,
    EpisodeCapabilitiesRequestEnvelope,
    EpisodeDispatchAuthenticator,
    EpisodeInferenceRequest,
    EpisodeServiceCapabilities,
    wire_digest,
)
from safe_agent.inference.episode_gateway import EpisodeInferenceDenied, EpisodeUnixTransport
from safe_agent.inference_service.episode_service import DurableServiceFence, create_episode_service
from safe_agent.inference_service.providers.openai_episode import (
    OpenAIEpisodeBackend,
    episode_system_instructions,
    openai_backend_capabilities,
)
from safe_agent.inference_service.socket_lifecycle import OwnedUnixListener
from tests.episodes.test_episode_foundation import NOW
from tests.episodes.test_episode_model_boundary import InProcessClient

MODEL = "capability_fixture_model"
DISPATCH_KEY = "dispatch_fixture_" + "a" * 40
PROVIDER_CANARY = "provider_fixture_secret_" + "b" * 40


def authenticator():
    return EpisodeDispatchAuthenticator(SecretStr(DISPATCH_KEY))


def challenge(**changes):
    return EpisodeCapabilitiesRequest(**{
        "nonce": "1" * 64, "model": MODEL, "issued_at": NOW,
        "deadline": NOW + timedelta(seconds=10), **changes,
    })


class FixtureBackend:
    def __init__(self):
        self.capabilities = EpisodeBackendCapabilities(
            profile="scripted_fixture_v1", model=MODEL, provider_origin=None,
            timeout_seconds=1.0, response_format="scripted_fixture",
            candidate_schema_hash=wire_digest(CANDIDATE_ADAPTER.json_schema()),
            system_instruction_hash=wire_digest("Explicitly scripted fixture"),
        )
        self.calls = 0

    def infer(self, _):
        self.calls += 1
        raise AssertionError("A readiness request must never dispatch a model")


def create_fixture(tmp_path, *, backend=None):
    backend = backend or FixtureBackend()
    fence = DurableServiceFence(tmp_path / "dispatch.sqlite")
    app = create_episode_service(model=MODEL, authenticator=authenticator(), backend=backend,
                                 fence=fence, clock=lambda: NOW)
    return app, backend, fence


def post(app, envelope):
    with InProcessClient(app, base_url="http://safe-episode") as client:
        return client.post("/v1/episode/capabilities", content=envelope.model_dump_json(),
                           headers={"Content-Type": "application/json"})


def test_signed_descriptor_is_read_only_and_does_not_claim_provider_access(tmp_path, monkeypatch):
    app, backend, fence = create_fixture(tmp_path)
    original = fence.path.read_bytes()

    def no_fence_write(*args, **kwargs):
        pytest.fail("Readiness must not claim or finish an inference request")

    monkeypatch.setattr(fence, "claim", no_fence_write)
    monkeypatch.setattr(fence, "finish", no_fence_write)
    auth = authenticator()
    request = challenge()
    response = post(app, auth.sign_capabilities_request(request))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    signed = EpisodeCapabilitiesReplyEnvelope.model_validate_json(response.content)
    reply = auth.verify_capabilities_reply(signed)
    assert reply.nonce == request.nonce and reply.request_hash == request.digest
    assert reply.deadline == request.deadline and reply.issued_at == NOW
    assert reply.capabilities == EpisodeServiceCapabilities(backend=backend.capabilities)
    assert not reply.capabilities.provider_access_verified
    assert backend.calls == 0 and fence.path.read_bytes() == original
    assert DISPATCH_KEY not in response.text and PROVIDER_CANARY not in response.text
    with sqlite3.connect(fence.path) as database:
        assert database.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)


@pytest.mark.parametrize("case", ["wrong_key", "expired", "future", "wrong_model", "oversized", "duplicate"])
def test_endpoint_denies_invalid_or_stale_challenges_without_dispatch(tmp_path, case):
    app, backend, fence = create_fixture(tmp_path)
    values = {}
    if case == "expired":
        values = {"issued_at": NOW - timedelta(seconds=11), "deadline": NOW - timedelta(seconds=1)}
    elif case == "future":
        values = {"issued_at": NOW + timedelta(seconds=1), "deadline": NOW + timedelta(seconds=11)}
    elif case == "wrong_model":
        values = {"model": "another_model"}
    auth = (EpisodeDispatchAuthenticator(SecretStr("wrong_key_" + "z" * 40))
            if case == "wrong_key" else authenticator())
    envelope = auth.sign_capabilities_request(challenge(**values))
    body = envelope.model_dump_json()
    if case == "oversized":
        body = " " * 8193
    elif case == "duplicate":
        body = '{"signature":"' + envelope.signature + '",' + body[1:]
    with InProcessClient(app, base_url="http://safe-episode") as client:
        response = client.post("/v1/episode/capabilities", content=body,
                               headers={"Content-Type": "application/json"})
    assert response.status_code == (413 if case == "oversized" else 403)
    assert response.content == b"" and backend.calls == 0
    with sqlite3.connect(fence.path) as database:
        assert database.execute("SELECT COUNT(*) FROM requests").fetchone() == (0,)


@pytest.mark.parametrize("seconds", [0, -1, 16, 3600])
def test_challenge_lifetime_cannot_exceed_fifteen_seconds(seconds):
    with pytest.raises(ValueError, match="fifteen seconds"):
        challenge(deadline=NOW + timedelta(seconds=seconds))


def test_unknown_backend_cannot_claim_configured_hosted_readiness(tmp_path):
    backend = SimpleNamespace(infer=lambda _: pytest.fail("Unexpected model call"))
    app, _, _ = create_fixture(tmp_path, backend=backend)
    response = post(app, authenticator().sign_capabilities_request(challenge()))
    assert response.status_code == 503 and response.content == b""


def test_service_descriptor_must_match_its_exact_configured_model(tmp_path):
    with pytest.raises(ValueError, match="MODEL_CONFIGURATION_MISMATCH"):
        create_episode_service(model="different_model", authenticator=authenticator(),
                               backend=FixtureBackend(), fence=DurableServiceFence(tmp_path / "mismatch.sqlite"))


def test_readiness_signatures_cannot_authorize_inference_or_reply():
    auth = authenticator()
    request = challenge()
    readiness = auth.sign_capabilities_request(request)
    inference = EpisodeInferenceRequest(
        request_id="infer_fixture", episode_id="ep_fixture", domain="retail", model=MODEL,
        policy_hash=wire_digest("policy"), observation_hash=wire_digest("observation"),
        projected_context={}, max_output_tokens=64, issued_at=NOW,
        deadline=NOW + timedelta(seconds=60),
    )
    dispatch = auth.sign(inference, wire_digest("reservation"))
    with pytest.raises(ValueError, match="AUTHENTICATION_FAILED"):
        auth.verify(dispatch.model_copy(update={"signature": readiness.signature}))
    with pytest.raises(ValueError, match="AUTHENTICATION_FAILED"):
        auth.verify_capabilities_request(readiness.model_copy(update={"signature": dispatch.signature}))
    reply = EpisodeCapabilitiesReply(
        nonce=request.nonce, request_hash=request.digest, issued_at=NOW, deadline=request.deadline,
        capabilities=EpisodeServiceCapabilities(backend=FixtureBackend().capabilities),
    )
    with pytest.raises(ValueError, match="AUTHENTICATION_FAILED"):
        auth.verify_capabilities_reply(EpisodeCapabilitiesReplyEnvelope(reply=reply, signature=readiness.signature))


@pytest.mark.parametrize("case", [
    "nonce", "request_hash", "deadline", "future_reply", "wrong_signature", "request_schema",
    "reply_schema", "candidate_schema", "instructions", "wrong_model", "fixture_profile",
])
def test_client_rejects_cross_challenge_stale_or_mismatched_capabilities(tmp_path, monkeypatch, case):
    auth = authenticator()
    expected = EpisodeServiceCapabilities(backend=openai_backend_capabilities(model=MODEL))
    transport = EpisodeUnixTransport(tmp_path / "unused.sock")

    def answer(route, body, *, max_reply_bytes):
        assert route == "/v1/episode/capabilities" and max_reply_bytes == 16_384
        request = auth.verify_capabilities_request(EpisodeCapabilitiesRequestEnvelope.model_validate_json(body))
        capabilities = expected
        if case in {"request_schema", "reply_schema"}:
            capabilities = expected.model_copy(update={case + "_hash": wire_digest("stale")})
        elif case in {"candidate_schema", "instructions", "wrong_model"}:
            field = {"candidate_schema": "candidate_schema_hash", "instructions": "system_instruction_hash",
                     "wrong_model": "model"}[case]
            changed = "different_model" if case == "wrong_model" else wire_digest("stale")
            capabilities = expected.model_copy(update={"backend": expected.backend.model_copy(update={field: changed})})
        elif case == "fixture_profile":
            capabilities = EpisodeServiceCapabilities(backend=FixtureBackend().capabilities)
        values = {"nonce": request.nonce, "request_hash": request.digest, "issued_at": NOW,
                  "deadline": request.deadline, "capabilities": capabilities}
        if case == "nonce":
            values["nonce"] = "f" * 64
        elif case == "request_hash":
            values["request_hash"] = wire_digest("another challenge")
        elif case == "deadline":
            values["deadline"] = request.deadline + timedelta(seconds=1)
        elif case == "future_reply":
            values["issued_at"] = NOW + timedelta(seconds=1)
        signed = auth.sign_capabilities_reply(EpisodeCapabilitiesReply(**values))
        if case == "wrong_signature":
            signed = signed.model_copy(update={"signature": "0" * 64})
        return signed.model_dump_json().encode()

    monkeypatch.setattr(transport, "_post", answer)
    with pytest.raises(EpisodeInferenceDenied) as failure:
        transport.capabilities(auth, expected=expected, clock=lambda: NOW)
    assert str(failure.value) == "EPISODE_CAPABILITIES_VERIFICATION_FAILED"
    assert failure.value.__cause__ is None


def test_capability_factory_is_pure_and_matches_real_backend_configuration(monkeypatch):
    openai = pytest.importorskip("openai")
    constructors = []
    client_options = []

    def client(**kwargs):
        constructors.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **_: pytest.fail("Readiness must not invoke a provider"))))

    monkeypatch.setattr(openai, "OpenAI", client)
    monkeypatch.setattr(openai, "DefaultHttpxClient", lambda **options: client_options.append(options))
    expected = openai_backend_capabilities(model=MODEL, timeout_seconds=12.0)
    assert not constructors and not client_options
    backend = OpenAIEpisodeBackend(model=MODEL, api_key=SecretStr(PROVIDER_CANARY), timeout_seconds=12.0)
    assert backend.capabilities == expected and len(constructors) == 1
    assert constructors[0]["base_url"] == expected.provider_origin
    assert constructors[0]["timeout"] == expected.timeout_seconds
    assert constructors[0]["max_retries"] == expected.provider_retries
    assert client_options == [{"follow_redirects": expected.follow_redirects, "trust_env": expected.trust_env}]
    assert expected.system_instruction_hash == wire_digest(episode_system_instructions())
    assert PROVIDER_CANARY not in expected.model_dump_json()


@contextmanager
def running_sidecar(app, path):
    import uvicorn

    path.parent.chmod(0o700)
    config = uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off",
                            proxy_headers=False, server_header=False, date_header=False)
    server = uvicorn.Server(config)
    with OwnedUnixListener(path) as listener:
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        timeout = time.monotonic() + 5
        try:
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= timeout:
                    pytest.fail("Local UDS fixture did not start")
                time.sleep(0.01)
            yield path
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive(), "Local UDS fixture failed to stop"


@pytest.fixture
def real_sidecar(tmp_path):
    app, backend, fence = create_fixture(tmp_path)
    with running_sidecar(app, tmp_path / "c.sock") as path:
        yield path, backend, fence


def test_real_uds_challenge_is_fresh_authenticated_and_has_zero_inference(real_sidecar):
    path, backend, fence = real_sidecar
    transport = EpisodeUnixTransport(path, timeout_seconds=2)
    expected = EpisodeServiceCapabilities(backend=backend.capabilities)
    original = fence.path.read_bytes()
    first = transport.capabilities(authenticator(), expected=expected, clock=lambda: NOW)
    second = transport.capabilities(authenticator(), expected=expected, clock=lambda: NOW)
    assert first.reply.nonce != second.reply.nonce
    assert first.reply.capabilities == second.reply.capabilities == expected
    assert authenticator().verify_capabilities_reply(first) == first.reply
    assert backend.calls == 0 and fence.path.read_bytes() == original
    assert "provider_fixture_secret" not in json.dumps(first.model_dump(mode="json"))


@pytest.mark.parametrize("target", ["socket", "parent"])
def test_real_uds_rejects_unprotected_filesystem_route(real_sidecar, target):
    path, backend, _ = real_sidecar
    changed = path if target == "socket" else path.parent
    original_mode = changed.stat().st_mode & 0o777
    changed.chmod(0o666 if target == "socket" else 0o755)
    try:
        with pytest.raises(EpisodeInferenceDenied, match="CAPABILITIES_VERIFICATION_FAILED"):
            EpisodeUnixTransport(path, timeout_seconds=2).capabilities(
                authenticator(), expected=EpisodeServiceCapabilities(backend=backend.capabilities), clock=lambda: NOW,
            )
    finally:
        changed.chmod(original_mode)
    assert backend.calls == 0


def test_preflight_uses_protected_key_and_real_hosted_sidecar_without_provider_request(tmp_path, monkeypatch):
    """Only inventory/provenance is authored; the complete service probe is real."""
    import httpx

    from safe_agent.benchmarks.tau3 import preflight
    from safe_agent.benchmarks.tau3.config import BenchmarkConfig

    openai = pytest.importorskip("openai")
    provider_requests = []

    def forbidden_provider(request):
        provider_requests.append(request.url)
        raise AssertionError("Preflight must never send a provider request")

    actual_http_client = openai.DefaultHttpxClient
    monkeypatch.setattr(openai, "DefaultHttpxClient", lambda **kwargs: actual_http_client(
        transport=httpx.MockTransport(forbidden_provider), **kwargs,
    ))
    config = BenchmarkConfig(output_dir=str(tmp_path / "new-run"),
                             development_task_ids=["authored_preflight"])
    backend = OpenAIEpisodeBackend(model=config.llm_agent, api_key=SecretStr(PROVIDER_CANARY))
    key_path = tmp_path / "dispatch-key"
    key_path.write_text(DISPATCH_KEY)
    key_path.chmod(0o600)
    socket_path = tmp_path / "p.sock"
    fence = DurableServiceFence(tmp_path / "preflight-fence.sqlite")
    original_fence = fence.path.read_bytes()
    app = create_episode_service(model=config.llm_agent, authenticator=authenticator(),
                                 backend=backend, fence=fence)
    monkeypatch.setattr(preflight, "inspect_benchmark", lambda *args: {
        "status": "ready", "source_verified": True, "selected_task_ids": ["authored_preflight"],
        "scheduled_conversations": 1, "selected_tasks_with_nl_assertions": 1,
    })
    monkeypatch.setattr(preflight, "_dependencies", lambda: {"authored_test": True})
    monkeypatch.setattr(preflight, "_safe_revision", lambda: {"commit": "a" * 40, "dirty": False})
    monkeypatch.setattr(preflight, "_pinned_judge", lambda root: (
        "gpt-4.1-2025-04-14", {"temperature": 0.0}, 3,
    ))
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "LITELLM_PROXY_API_BASE", "LITELLM_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    harness_canary = "synthetic_harness_key_not_sent_anywhere"
    monkeypatch.setenv("OPENAI_API_KEY", harness_canary)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    monkeypatch.setenv("SAFE_EPISODE_SOCKET", str(socket_path))
    monkeypatch.setenv("SAFE_EPISODE_DISPATCH_KEY_FILE", str(key_path))
    try:
        with running_sidecar(app, socket_path):
            report = preflight.preflight_run(config, tau_root=tmp_path)
            assert report["status"] == "ready_for_attempt" and report["ready_for_attempt"]
            assert report["sidecar"]["verified"]
            expected = EpisodeServiceCapabilities(backend=openai_backend_capabilities(model=config.llm_agent))
            assert report["sidecar"]["configuration"] == expected.model_dump(mode="json")
            assert not report["sidecar"]["provider_access_verified"]
            assert report["model_calls"] == report["provider_requests"] == 0
            wrong_model = preflight.preflight_run(
                config.model_copy(update={"llm_agent": "gpt-different-configured-model"}), tau_root=tmp_path,
            )
            assert wrong_model["status"] == "blocked"
            assert "SIDECAR_CAPABILITY_VERIFICATION_FAILED" in wrong_model["issues"]
            key_path.chmod(0o644)
            bad_key_file = preflight.preflight_run(config, tau_root=tmp_path)
            assert bad_key_file["status"] == "blocked"
            assert "SIDECAR_CAPABILITY_VERIFICATION_FAILED" in bad_key_file["issues"]
            key_path.chmod(0o600)
    finally:
        backend._client.close()
    assert not provider_requests and fence.path.read_bytes() == original_fence
    assert not (tmp_path / "new-run").exists()
    encoded = json.dumps([report, wrong_model, bad_key_file])
    assert all(secret not in encoded for secret in (DISPATCH_KEY, PROVIDER_CANARY, harness_canary))
