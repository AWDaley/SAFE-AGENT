"""M7-04 independent inference-only ASGI service security acceptance."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import sha256
from safe_agent.contracts.inference import InferenceRequest
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.inference_service.service import (
    TrustedInferenceServicePolicy,
    TrustedWorkloadAuthenticator,
    create_inference_service,
)
from tests.m2.conftest import NOW
from tests.m7.test_inference_gateway import fixture

KEY = "m7_synthetic_workload_only_not_a_real_provider_key_0123456789"
HOST = "inference.example.com"


def valid_request(*, ident="a", now=NOW):
    ctx, projection, _ = fixture()
    return InferenceRequest(
        request_id="inf_" + ident * 32, route_id="route_test_only",
        model_revision="mock_v1",
        prompt_digest=sha256({
            "instruction": PLANNER_INSTRUCTION,
            "task": ctx, "availability": projection,
        }),
        issued_at=now, deadline_at=now + timedelta(seconds=10),
        task=ctx, availability=projection,
    )


def setup(*, pol=None, backend=None, clock=lambda: NOW, ticker=None):
    policy = pol or TrustedInferenceServicePolicy(
        route_id="route_test_only", model_revision="mock_v1",
        expected_host=HOST,
    )
    auth = TrustedWorkloadAuthenticator(SecretStr(KEY))
    app = create_inference_service(
        policy, auth, backend=backend,
        clock=clock, monotonic=ticker or (lambda: 10.0),
    )
    client = TestClient(app)
    headers = {
        "Host": HOST, "Authorization": "Bearer " + KEY,
        "Content-Type": "application/json",
    }
    return client, headers


def test_offline_only_routes_are_minimal_and_no_execution_api():
    client, headers = setup()
    assert client.get("/healthz").json() == {
        "status": "ready", "mode": "offline_mock_only",
    }
    for path in ("/docs", "/openapi.json", "/redoc", "/v1/tasks",
                 "/v1/inference", "/v1/google/events"):
        assert client.get(path).status_code == 404
        assert client.post(path, headers=headers, json={}).status_code == 404


def test_strict_authentication_before_reading_model_payload():
    client, headers = setup()
    route = "/v1/infer"
    request = valid_request().model_dump(mode="json")
    for wrong in (
        {}, {"Host": HOST}, {"Host": HOST, "Authorization": "Bearer bad"},
        {**headers, "Host": "attacker.example.com"},
        {**headers, "Authorization": "Basic " + KEY},
        {**headers, "Authorization": "Bearer " + KEY + "\nInjected: bad"},
    ):
        response = client.post(route, headers=wrong, json=request)
        assert response.status_code == 401
        assert not response.content
        assert KEY not in response.text
    assert client.get("/internal/status").status_code == 401
    assert client.get("/internal/status", headers=headers).status_code == 200
    with pytest.raises(ValueError):
        TrustedWorkloadAuthenticator(SecretStr("short"))
    with pytest.raises(TypeError):
        TrustedWorkloadAuthenticator(KEY)  # type: ignore[arg-type]


def test_valid_request_is_unprivileged_and_replay_is_denied():
    client, headers = setup()
    response = client.post(
        "/v1/infer", headers=headers, json=valid_request().model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["schema_version"] == "safe_agent.inference_transport_reply.v1"
    assert data["request_id"] == "inf_" + "a" * 32
    proposal = json.loads(data["output_json"])
    assert proposal["operation"] == "calendar.create_event"
    assert data["reported_usage"]["source"] == "synthetic_mock"
    assert data["reported_usage"]["independently_verified"] is False
    assert response.headers["cache-control"] == "no-store"
    assert KEY not in response.text

    repeated = client.post(
        "/v1/infer", headers=headers, json=valid_request().model_dump(mode="json"),
    )
    assert repeated.status_code == 409
    status = client.get("/internal/status", headers=headers).json()
    assert status["accepted"] == status["completed"] == 1
    assert status["refused"] == 1
    assert status["active"] == 0
    assert status["real_provider_enabled"] is False
    assert status["business_effect_performed"] is False
    for private in ("ref_customer", "cal_main_private", "cust_c1", KEY):
        assert private not in str(status)


@pytest.mark.parametrize("payload", [
    b"[]",
    b"not json",
    b'{"request_id":"inf_' + b"a" * 32 + b'","request_id":"inf_'
    + b"b" * 32 + b'"}',
    b'{"input_tokens":NaN}',
    b'{"path":"/v1/tools"}',
])
def test_bad_json_and_duplicate_input_rejected_before_backend(payload):
    client, headers = setup()
    response = client.post("/v1/infer", headers=headers, content=payload)
    assert response.status_code == 400
    assert not response.content


def test_body_length_and_media_controls_fail_closed():
    client, headers = setup(pol=TrustedInferenceServicePolicy(
        route_id="route_test_only", model_revision="mock_v1",
        expected_host=HOST, max_request_bytes=1024,
    ))
    assert client.post("/v1/infer", headers=headers, content=b"x" * 1040).status_code == 413
    assert client.post(
        "/v1/infer", headers={**headers, "Content-Type": "text/plain"},
        content=b"{}",
    ).status_code == 415
    assert client.post(
        "/v1/infer", headers={**headers, "Content-Encoding": "gzip"},
        content=b"{}",
    ).status_code == 415


def test_invalid_scope_or_expired_request_fails_closed():
    client, headers = setup()
    original = valid_request()
    for update in (
        {"route_id": "outside_scope"},
        {"model_revision": "alternate_model"},
        {"prompt_digest": "sha256:" + "0" * 64},
        {"deadline_at": NOW - timedelta(minutes=1)},
        {"issued_at": NOW + timedelta(minutes=1)},
    ):
        changed = original.model_copy(update=update)
        response = client.post(
            "/v1/infer", headers=headers,
            json=changed.model_dump(mode="json"),
        )
        assert response.status_code in (400, 429)
    # The rejected inputs do not consume the valid request ID.
    assert client.post(
        "/v1/infer", headers=headers, json=original.model_dump(mode="json"),
    ).status_code == 200


def test_rate_limit_limits_new_inference_requests():
    clock = [5.0]
    client, headers = setup(pol=TrustedInferenceServicePolicy(
        route_id="route_test_only", model_revision="mock_v1",
        expected_host=HOST, max_requests_per_window=1,
    ), ticker=lambda: clock[0])
    assert client.post(
        "/v1/infer", headers=headers, json=valid_request().model_dump(mode="json"),
    ).status_code == 200
    assert client.post(
        "/v1/infer", headers=headers, json=valid_request(ident="b").model_dump(mode="json"),
    ).status_code == 429
    clock[0] += 61
    assert client.post(
        "/v1/infer", headers=headers, json=valid_request(ident="b").model_dump(mode="json"),
    ).status_code == 200


def test_concurrent_admission_fails_closed_without_running_second_inference(monkeypatch):
    started = threading.Event()
    permit = threading.Event()
    backend = OfflineMockInferenceTransport()
    original = backend.infer
    count = []

    def deliberately_block(request):
        count.append(request.request_id)
        started.set()
        assert permit.wait(3)
        return original(request)

    monkeypatch.setattr(backend, "infer", deliberately_block)
    client, headers = setup(backend=backend, pol=TrustedInferenceServicePolicy(
        route_id="route_test_only", model_revision="mock_v1",
        expected_host=HOST, max_inflight=1,
    ))
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(lambda: client.post(
            "/v1/infer", headers=headers,
            json=valid_request().model_dump(mode="json"),
        ))
        assert started.wait(3)
        other = client.post(
            "/v1/infer", headers=headers,
            json=valid_request(ident="b").model_dump(mode="json"),
        )
        assert other.status_code == 429
        permit.set()
        assert future.result(timeout=4).status_code == 200
    assert count == ["inf_" + "a" * 32]


def test_backend_exception_or_late_completion_is_redacted_and_replay_blocked(monkeypatch):
    backend = OfflineMockInferenceTransport()
    def broken(request):
        raise RuntimeError("PRIVATE_GOOGLE_REFRESH_TOKEN")

    monkeypatch.setattr(backend, "infer", broken)
    client, headers = setup(backend=backend)
    answer = client.post(
        "/v1/infer", headers=headers, json=valid_request().model_dump(mode="json"),
    )
    assert answer.status_code == 503
    assert not answer.content
    assert "PRIVATE" not in answer.text
    assert client.post(
        "/v1/infer", headers=headers, json=valid_request().model_dump(mode="json"),
    ).status_code == 409

    # New service where a backend reply arrives after its task deadline.
    later = [NOW]
    mock = OfflineMockInferenceTransport()
    original = mock.infer
    def slow(request):
        later[0] += timedelta(seconds=20)
        return original(request)
    monkeypatch.setattr(mock, "infer", slow)
    second, headers = setup(backend=mock, clock=lambda: later[0])
    assert second.post(
        "/v1/infer", headers=headers,
        json=valid_request(ident="c").model_dump(mode="json"),
    ).status_code == 503


def test_policy_backend_and_status_are_closed():
    with pytest.raises(ValidationError):
        TrustedInferenceServicePolicy(
            route_id="r", model_revision="m", expected_host="testserver",
            model_provider_url="https://evil.example.com",
        )
    with pytest.raises(ValidationError):
        TrustedInferenceServicePolicy(
            route_id="r", model_revision="m",
            expected_host=HOST, max_inflight=0,
        )
    policy = TrustedInferenceServicePolicy(
        route_id="r", model_revision="m", expected_host=HOST,
    )
    auth = TrustedWorkloadAuthenticator(SecretStr(KEY))
    with pytest.raises(TypeError):
        create_inference_service(policy, auth, backend=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        create_inference_service(policy, object())  # type: ignore[arg-type]
