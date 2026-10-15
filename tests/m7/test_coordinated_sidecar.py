"""Closed coordinated UDS boundary: authentication before parsing and no raw route."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from safe_agent.contracts.inference_coordinator import TrustedDispatchAuthenticator
from safe_agent.inference.coordinated_uds import CoordinatedUDSInferenceTransport
from safe_agent.inference_service.service import (
    TrustedInferenceServicePolicy,
    TrustedWorkloadAuthenticator,
    create_inference_service,
)
from tests.m7.test_inference_coordinator import configured, repo  # noqa: F401

WORKLOAD = SecretStr("w" * 64)
SIGNING = SecretStr("s" * 64)
HOST = "inference.example.com"


def fixture(repo):
    args, _, _, store = configured(repo)
    claim = store.claim(*args)
    req, marker = store.start(*args, claim)
    signer = TrustedDispatchAuthenticator(SIGNING)
    envelope = signer.seal(req, claim, marker)
    app = create_inference_service(TrustedInferenceServicePolicy(
        route_id=req.route_id, model_revision=req.model_revision, expected_host=HOST,
    ), TrustedWorkloadAuthenticator(WORKLOAD), dispatch_authenticator=signer,
        clock=lambda: req.issued_at)
    headers = {"Host": HOST, "Authorization": "Bearer " + WORKLOAD.get_secret_value(),
               "Content-Type": "application/json"}
    return TestClient(app), headers, envelope


def test_coordinated_mode_accepts_signed_request_and_has_no_raw_route(repo):
    client, headers, envelope = fixture(repo)
    assert client.post("/v1/infer", headers=headers, content=envelope.request.model_dump_json()).status_code == 404
    for endpoint in ("/v1/tasks", "/v1/google/events", "/v1/billing", "/docs"):
        assert client.get(endpoint).status_code == 404
    result = client.post("/v1/dispatch", headers=headers, content=envelope.model_dump_json())
    assert result.status_code == 200
    assert WORKLOAD.get_secret_value() not in result.text
    assert SIGNING.get_secret_value() not in result.text
    assert envelope.signature not in result.text
    assert client.post("/v1/dispatch", headers=headers, content=envelope.model_dump_json()).status_code == 409


@pytest.mark.parametrize("failure", ["missing_auth", "bad_signature", "duplicate_key", "raw_request", "wrong_marker", "wrong_host"])
def test_forged_dispatch_does_not_consume_backend(repo, failure):
    client, headers, envelope = fixture(repo)
    data = envelope.model_dump_json()
    if failure == "missing_auth":
        headers.pop("Authorization")
    elif failure == "bad_signature":
        data = envelope.model_copy(update={"signature": "0" * 64}).model_dump_json()
    elif failure == "duplicate_key":
        data = data[:-1] + ',"claim_version":999}'
    elif failure == "raw_request":
        data = envelope.request.model_dump_json()
    elif failure == "wrong_marker":
        data = envelope.model_copy(update={"dispatch_event_hash": "sha256:" + "1" * 64}).model_dump_json()
    else:
        headers["Host"] = "attacker.example.com"
    response = client.post("/v1/dispatch", headers=headers, content=data)
    assert response.status_code in (400, 401)
    assert not response.content
    good_headers = {**headers, "Host": HOST, "Authorization": "Bearer " + WORKLOAD.get_secret_value()}
    status = client.get("/internal/status", headers=good_headers).json()
    assert status["accepted"] == status["completed"] == 0


def test_key_separation_and_secret_validation():
    with pytest.raises(ValueError):
        CoordinatedUDSInferenceTransport(Path("/tmp/unused.sock"), expected_host=HOST,
                                        workload_token=WORKLOAD, dispatch_key=WORKLOAD)
    for key in ("too_short", "a" * 63 + "\n", "é" * 64):
        with pytest.raises(ValueError):
            TrustedDispatchAuthenticator(SecretStr(key))
    with pytest.raises(TypeError):
        TrustedDispatchAuthenticator("not_a_secret_type")
    with pytest.raises(ValidationError):
        from safe_agent.contracts.inference_coordinator import CoordinatedInferenceEnvelope
        CoordinatedInferenceEnvelope.model_validate({"tools": ["execute"]})
