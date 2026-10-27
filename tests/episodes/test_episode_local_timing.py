"""Finite local timing and loss boundaries; authored bytes, no model calls."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from safe_agent.benchmarks.tau3.config import (
    BenchmarkConfig,
    effective_inference_timing,
    harness_model_settings,
)
from safe_agent.benchmarks.tau3.local_harness import harness_transport
from safe_agent.canonical import sha256
from safe_agent.contracts.episode_inference import (
    EpisodeServiceCapabilities,
    LocalTimingProfile,
    wire_digest,
)
from safe_agent.contracts.episodes import EpisodeState
from safe_agent.inference.episode_gateway import (
    EpisodeInferenceDenied,
    EpisodeInferenceGateway,
    EpisodeUnixTransport,
    ScriptedEpisodeTransport,
)
from safe_agent.inference_service.episode_service import DurableServiceFence, create_episode_service
from safe_agent.inference_service.providers.local_openai_episode import (
    LocalOpenAIEpisodeBackend,
    local_openai_backend_capabilities,
)
from safe_agent.orchestration.episode_runtime import EpisodeRuntime
from tests.episodes.test_episode_capabilities import authenticator, running_sidecar
from tests.episodes.test_episode_foundation import NOW
from tests.episodes.test_episode_local_backend import BASE, MODEL, completion, request
from tests.episodes.test_episode_model_boundary import (
    ANSWER,
    InProcessClient,
    reply,
)
from tests.episodes.test_episode_model_boundary import boundary as _model_boundary
from tests.episodes.test_episode_model_boundary import (
    request as boundary_request,
)
from tests.episodes.test_episode_runtime_audit import customer, runtime_fixture, start

PROFILE = LocalTimingProfile(provider_timeout_seconds=90.0, transport_timeout_seconds=105.0,
                             request_timeout_seconds=115.0)


@pytest.fixture
def boundary(tmp_path):
    yield from _model_boundary.__wrapped__(tmp_path)


@pytest.mark.parametrize("changes", [
    {"provider_timeout_seconds": 0.0}, {"provider_timeout_seconds": True},
    {"provider_timeout_seconds": float("nan")}, {"transport_timeout_seconds": float("inf")},
    {"transport_timeout_seconds": 90.0}, {"transport_timeout_seconds": 116.0},
    {"request_timeout_seconds": 121.0},
])
def test_timing_rejects_infinite_coerced_or_unordered_allowances(changes):
    with pytest.raises(ValueError):
        LocalTimingProfile.model_validate(PROFILE.model_dump() | changes)


def test_legacy_serialization_and_timeout_condition_are_retained():
    config = BenchmarkConfig(model_route="local", local_api_base=BASE)
    assert "local_timing" not in config.model_dump()
    assert "stop_after_infrastructure_errors" not in config.model_dump()
    descriptor = local_openai_backend_capabilities(model=MODEL, base_url=BASE)
    assert "local_timing" not in descriptor.model_dump()
    assert descriptor.timeout_seconds == 60
    timing = effective_inference_timing(config)
    assert (timing["provider_timeout_seconds"], timing["transport_timeout_seconds"],
            timing["request_timeout_seconds"]) == (60, 60, 90)
    assert timing["harness_provider_timeout_seconds"] is None
    assert "timeout" not in harness_model_settings(config, "user")[1]


def test_explicit_profile_is_bound_and_routes_same_provider_limit_to_user_and_stock():
    config = BenchmarkConfig(model_route="local", local_api_base=BASE, local_timing=PROFILE,
                             system="llm_agent")
    assert harness_model_settings(config, "agent")[1]["timeout"] == 90
    assert harness_model_settings(config, "user")[1]["timeout"] == 90
    assert effective_inference_timing(config)["absolute_local_deadline_enforced"] is True
    with pytest.raises(ValueError, match="LOCAL_TIMING_CAPABILITY_MISMATCH"):
        local_openai_backend_capabilities(model=MODEL, base_url=BASE, timeout_seconds=60,
                                         local_timing=PROFILE)
    with pytest.raises(ValueError):
        BenchmarkConfig(local_timing=PROFILE)
    with pytest.raises(ValueError):
        BenchmarkConfig(stage="coverage", stop_after_infrastructure_errors=1)


def test_real_capability_exchange_binds_all_three_deadlines(tmp_path):
    backend = LocalOpenAIEpisodeBackend(model=MODEL, base_url=BASE, local_timing=PROFILE)
    app = create_episode_service(model=MODEL, authenticator=authenticator(), backend=backend,
        fence=DurableServiceFence(tmp_path / "capability.sqlite"), clock=lambda: NOW)
    try:
        with running_sidecar(app, tmp_path / "timing.sock") as path:
            transport = EpisodeUnixTransport(path, timeout_seconds=2)
            expected = EpisodeServiceCapabilities(backend=backend.capabilities)
            result = transport.capabilities(authenticator(), expected=expected, clock=lambda: NOW,
                                            absolute_timeout_seconds=2.0)
            assert result.reply.capabilities.backend.local_timing == PROFILE
            changed = PROFILE.model_copy(update={"transport_timeout_seconds": 106.0})
            wrong = EpisodeServiceCapabilities(backend=local_openai_backend_capabilities(
                model=MODEL, base_url=BASE, local_timing=changed,
            ))
            with pytest.raises(EpisodeInferenceDenied, match="CAPABILITIES_VERIFICATION_FAILED"):
                transport.capabilities(authenticator(), expected=wrong, clock=lambda: NOW)
    finally:
        backend.close()


def test_local_capability_trickled_body_has_total_deadline_without_inference(tmp_path):
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    observed = {"requests": 0, "cancelled": threading.Event()}

    async def capability_response(request):
        assert request.method == "POST"
        observed["requests"] += 1

        async def trickled_body():
            try:
                for _ in range(200):
                    yield b" "
                    await asyncio.sleep(0.03)
            finally:
                observed["cancelled"].set()

        return StreamingResponse(trickled_body(), media_type="application/json")

    app = Starlette(routes=[Route("/v1/episode/capabilities", capability_response, methods=["POST"])])
    expected = EpisodeServiceCapabilities(backend=local_openai_backend_capabilities(model=MODEL, base_url=BASE))
    with running_sidecar(app, tmp_path / "trickled-capabilities.sock") as path:
        transport = EpisodeUnixTransport(path, timeout_seconds=5)
        started = time.monotonic()
        with pytest.raises(EpisodeInferenceDenied, match="CAPABILITIES_VERIFICATION_FAILED"):
            transport.capabilities(authenticator(), expected=expected, absolute_timeout_seconds=0.15)
        assert time.monotonic() - started < 0.55
        assert observed["requests"] == 1
        assert observed["cancelled"].wait(1)


def test_service_rejects_shortened_profile_before_fence_or_provider(tmp_path):
    backend = LocalOpenAIEpisodeBackend(model=MODEL, base_url=BASE, local_timing=PROFILE)
    backend.infer = lambda _: pytest.fail("Mismatched lifetime reached provider")
    fence = DurableServiceFence(tmp_path / "lifetime.sqlite")
    app = create_episode_service(model=MODEL, authenticator=authenticator(), backend=backend,
                                 fence=fence, clock=lambda: NOW)
    try:
        with InProcessClient(app, base_url="http://safe-episode") as client:
            envelope = authenticator().sign(request(), wire_digest("reservation"))
            response = client.post("/v1/episode/infer", content=envelope.model_dump_json(),
                                   headers={"Content-Type": "application/json"})
            assert response.status_code == 403
            assert response.headers["x-safe-inference-reason"] == "EPISODE_INFERENCE_LIFETIME_INSUFFICIENT"
        with sqlite3.connect(fence.path) as db:
            assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    finally:
        backend.close()


@pytest.mark.parametrize("advance,allowed", [(0, True), (10, False)])
def test_runtime_admits_only_the_complete_declared_lifetime(tmp_path, advance, allowed):
    original, transport, context, tools, engine = runtime_fixture(tmp_path)
    clock = [NOW]
    context = context.model_copy(update={"run_id": "timed_run", "local_timing": PROFILE,
                                          "episode_budget": {"deadline_seconds": 120}})
    runtime = EpisodeRuntime(context, engine=engine, transport=transport,
                             admission_evaluator=original._admission_evaluator, clock=lambda: clock[0])
    try:
        state = start(runtime, tools)
        clock[0] += timedelta(seconds=advance)
        if allowed:
            runtime.step(customer("Authored readiness question.", "observation_timed"), state)
            req = transport.requests[0]
            assert (req.deadline - req.issued_at).total_seconds() == 115
        else:
            with pytest.raises(EpisodeInferenceDenied, match="LIFETIME_INSUFFICIENT"):
                runtime.step(customer("Authored readiness question.", "observation_timed"), state)
            assert not transport.requests
            assert runtime.episodes.get_run(runtime.run_id).usage.model_calls == 0
    finally:
        engine.dispose()


def test_late_reply_retains_hold_and_cannot_redispatch_after_restart(boundary):
    episodes, journal, auth, view = boundary
    clock = [NOW]
    calls = []
    transport = ScriptedEpisodeTransport([ANSWER])
    req = boundary_request(view)

    def late(envelope):
        calls.append(envelope.request.request_id)
        clock[0] = req.deadline + timedelta(microseconds=1)
        return reply(envelope.request)

    transport.infer = late
    gateway = EpisodeInferenceGateway(episodes=episodes, journal=journal, transport=transport,
                                       authenticator=auth, clock=lambda: clock[0])
    with pytest.raises(EpisodeInferenceDenied) as failure:
        gateway.infer(req, token_hold=300, cost_hold_microusd=10)
    assert failure.value.reason_code == "EPISODE_INFERENCE_REPLY_LATE"
    assert journal.inference(req.request_id)[2] == "UNCERTAIN"
    assert episodes.inspect(view.episode_id).state is EpisodeState.OUTCOME_UNCERTAIN
    assert episodes.get_run(view.run_id).usage.cost_microusd == 10
    assert sha256("EPISODE_INFERENCE_REPLY_LATE") in str(episodes.events(view.episode_id))
    clock[0] = NOW
    restarted = EpisodeInferenceGateway(episodes=episodes, journal=journal, transport=transport,
                                         authenticator=auth, clock=lambda: clock[0])
    with pytest.raises(EpisodeInferenceDenied):
        restarted.infer(req, token_hold=300, cost_hold_microusd=10)
    assert len(calls) == 1


@contextmanager
def delayed_http(mode):
    state = {"requests": 0, "methods": [], "entered": threading.Event(), "closed": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["requests"] += 1
            state["methods"].append("POST")
            state["entered"].set()
            try:
                if mode == "disconnect":
                    self.connection.settimeout(2)
                    if self.connection.recv(1) == b"":
                        state["closed"].set()
                    return
                if mode == "headers":
                    time.sleep(0.65)
                payload = json.dumps(completion()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if mode == "body":
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.wfile.write(payload)
            except OSError:
                state["closed"].set()

        def do_GET(self):
            state["requests"] += 1
            state["methods"].append("GET")
            state["entered"].set()
            payload = json.dumps({"data": [{"id": MODEL}]}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                for byte in payload:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                state["closed"].set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def timed_request(profile, *, request_id="timed_request"):
    now = datetime.now(UTC)
    return request(request_id=request_id, issued_at=now,
                   deadline=now + timedelta(seconds=profile.request_timeout_seconds))


def test_explicit_profile_roundtrip_returns_one_bound_reply_and_releases_capacity(tmp_path):
    profile = LocalTimingProfile(provider_timeout_seconds=2.0, transport_timeout_seconds=3.0,
                                 request_timeout_seconds=4.0)
    with delayed_http("good") as (base, observed):
        backend = LocalOpenAIEpisodeBackend(model=MODEL, base_url=base, local_timing=profile)
        fence = DurableServiceFence(tmp_path / "complete.sqlite")
        app = create_episode_service(model=MODEL, authenticator=authenticator(), backend=backend,
                                     fence=fence)
        try:
            with running_sidecar(app, tmp_path / "complete.sock") as path:
                transport = EpisodeUnixTransport(path, local_timing=profile)
                for index in range(2):
                    req = timed_request(profile, request_id=f"timed_request_{index}")
                    result = transport.infer(authenticator().sign(req, wire_digest("reservation")))
                    assert result.request_id == req.request_id
                    assert result.input_hash == req.digest
                    assert result.model == MODEL and result.output_tokens == 8
                    assert not backend._active
            assert observed["requests"] == 2
            with sqlite3.connect(fence.path) as db:
                assert db.execute("SELECT COUNT(*) FROM requests WHERE reply_json IS NOT NULL").fetchone()[0] == 2
        finally:
            backend.close()


@pytest.mark.parametrize("mode", ["headers", "body"])
def test_total_provider_deadline_closes_stalled_or_trickled_response(mode):
    profile = LocalTimingProfile(provider_timeout_seconds=0.15, transport_timeout_seconds=0.35,
                                 request_timeout_seconds=0.5)
    with delayed_http(mode) as (base, observed):
        backend = LocalOpenAIEpisodeBackend(model=MODEL, base_url=base, local_timing=profile)
        try:
            started = time.monotonic()
            with pytest.raises(ValueError) as failure:
                backend.infer(timed_request(profile))
            assert time.monotonic() - started < 0.55
            assert failure.value.reason_code == "LOCAL_MODEL_RESPONSE_DEADLINE_EXCEEDED"
            assert observed["requests"] == 1
            assert not backend._active
        finally:
            backend.close()


def test_user_stock_transport_uses_same_absolute_bound_even_if_sdk_requests_longer(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
    profile = LocalTimingProfile(provider_timeout_seconds=0.15, transport_timeout_seconds=0.35,
                                 request_timeout_seconds=0.5)
    with delayed_http("body") as (base, observed):
        config = BenchmarkConfig(model_route="local", local_api_base=base, local_timing=profile,
                                 system="llm_agent")
        with harness_transport(config):
            import litellm

            started = time.monotonic()
            with pytest.raises(httpx.TimeoutException):
                litellm.client_session.post(base + "/chat/completions", json={"model": MODEL},
                    headers={"Authorization": "Bearer safe-local-no-key"}, timeout=999)
            assert time.monotonic() - started < 0.55
        assert observed["requests"] == 1


def test_preflight_metadata_trickled_body_has_total_deadline_without_inference():
    from safe_agent.benchmarks.tau3.preflight import _probe_local_models

    with delayed_http("body") as (base, observed):
        config = BenchmarkConfig(model_route="local", local_api_base=base,
                                 llm_agent=MODEL, llm_user=MODEL)
        started = time.monotonic()
        with pytest.raises(httpx.TimeoutException):
            _probe_local_models(config, timeout_seconds=0.15)
        assert time.monotonic() - started < 0.55
        assert observed["requests"] == 1
        assert observed["methods"] == ["GET"]


def test_caller_disconnect_closes_only_its_provider_socket_and_keeps_restart_fence(tmp_path):
    profile = LocalTimingProfile(provider_timeout_seconds=2.0, transport_timeout_seconds=3.0,
                                 request_timeout_seconds=4.0)
    with delayed_http("disconnect") as (base, observed):
        backend = LocalOpenAIEpisodeBackend(model=MODEL, base_url=base, local_timing=profile)
        fence_path = tmp_path / "cancel.sqlite"
        path = tmp_path / "cancel.sock"
        auth = authenticator()
        req = timed_request(profile)
        envelope = auth.sign(req, wire_digest("reservation"))
        body = envelope.model_dump_json().encode()

        def app():
            return create_episode_service(model=MODEL, authenticator=auth, backend=backend,
                                          fence=DurableServiceFence(fence_path))

        try:
            with running_sidecar(app(), path):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1)
                client.connect(str(path))
                client.sendall(("POST /v1/episode/infer HTTP/1.1\r\nHost: safe-episode\r\n"
                                "Content-Type: application/json\r\n"
                                f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
                assert observed["entered"].wait(1)
                client.shutdown(socket.SHUT_RDWR)
                client.close()
                assert observed["closed"].wait(1), "Caller loss left the provider socket active"
            with running_sidecar(app(), path):
                with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), timeout=1,
                                  trust_env=False) as client:
                    replay = client.post("http://safe-episode/v1/episode/infer", content=body,
                                          headers={"Content-Type": "application/json"})
                    assert replay.status_code == 409
            assert observed["requests"] == 1
            with sqlite3.connect(fence_path) as db:
                assert db.execute("SELECT COUNT(*),MAX(reply_json) FROM requests").fetchone() == (1, None)
        finally:
            backend.close()
