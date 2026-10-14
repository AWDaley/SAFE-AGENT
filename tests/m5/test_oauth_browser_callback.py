"""OAuth test-only callback; all Google communication is simulated by google_kit."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from safe_agent.integrations.google_calendar.http import create_google_test_router
from safe_agent.security.principal import SignedOperatorTokens
from tests.m5.conftest import CALENDAR, NOW


def _make_client(kit):
    tokens = SignedOperatorTokens(b"TEST_ONLY_GOOGLE_CALLBACK_OPERATOR_KEY_32_bytes!")
    app = FastAPI()
    app.include_router(create_google_test_router(
        kit["oauth"], tokens, clock=lambda: NOW,
    ))
    principal = kit["principal"]
    bearer = tokens.issue_for_trusted_operator(principal)
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    return client, {"Authorization": "Bearer " + bearer}


def test_bearer_authenticated_browser_consent_and_replay(google_kit):
    client, headers = _make_client(google_kit)
    assert client.post("/v1/integrations/google-test/authorize",
                       json={"calendar_id": CALENDAR}).status_code == 401
    started = client.post(
        "/v1/integrations/google-test/authorize",
        json={"calendar_id": CALENDAR}, headers=headers,
    )
    assert started.status_code == 200
    assert started.headers["cache-control"] == "no-store"
    url = started.json()["authorization_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert "test_client_secret" not in started.text
    assert "code_verifier" not in started.text
    callback = client.get(
        "/oauth/google/callback",
        params={"state": state, "code": "authorization_code_abc"},
    )
    assert callback.status_code == 200
    assert "test_refresh_token" not in callback.text
    assert callback.headers["cache-control"] == "no-store"
    assert client.get(
        "/oauth/google/callback",
        params={"state": state, "code": "authorization_code_abc"},
    ).status_code == 400
    assert google_kit["sim"].insert_calls == 0


def test_callback_cookie_mismatch_blocks_exchange(google_kit):
    client, headers = _make_client(google_kit)
    client.post("/v1/integrations/google-test/authorize",
                json={"calendar_id": CALENDAR}, headers=headers)
    result = client.get("/oauth/google/callback", params={
        "state": "A" * 48, "code": "authorization_code_abc",
    })
    assert result.status_code == 400
    assert google_kit["sim"].auth_calls == 0


def test_refuse_nonallowlisted_calendar_and_weaker_operator(google_kit):
    client, headers = _make_client(google_kit)
    denied = client.post(
        "/v1/integrations/google-test/authorize",
        json={"calendar_id": "other@group.calendar.google.com"},
        headers=headers,
    )
    assert denied.status_code == 403
    assert google_kit["sim"].auth_calls == 0
    assert google_kit["sim"].insert_calls == 0


def test_disconnect_revokes_locally_and_reports_remote_uncertainty(google_kit):
    kit = google_kit
    client, headers = _make_client(kit)
    kit["sim"].fail = "revoke"
    result = client.post(
        f"/v1/integrations/google-test/{kit['connection']}/disconnect",
        headers=headers,
    )
    assert result.status_code == 200
    assert result.json() == {
        "local_credentials_revoked": True, "remote_revocation_confirmed": False,
    }
    assert kit["sim"].insert_calls == 0
