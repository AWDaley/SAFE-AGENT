from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from safe_agent.integrations.google_calendar import (
    GoogleCalendarError, GoogleCalendarTestAdapter, GoogleCalendarUnavailable, HttpResponse,
)
from safe_agent.integrations.oauth import build_google_test_authorization


def test_test_account_required() -> None:
    with pytest.raises(ValueError):
        GoogleCalendarTestAdapter(calendar_id="test@example.com", transport=lambda *args: None)


def test_readback_allowlist_and_encoded_id() -> None:
    calls = []
    def transport(method, url, headers, body):
        calls.append((method, url, body))
        return HttpResponse(200, {"id": "event123", "etag": "abc", "status": "confirmed", "summary": "PRIVATE", "attendees": ["private@example.com"]})
    adapter = GoogleCalendarTestAdapter(calendar_id="test@example.com", transport=transport, test_mode=True)
    assert adapter.get_event("event123") == {"id": "event123", "etag": "abc", "status": "confirmed"}
    assert "%40" in calls[0][1] and calls[0][0] == "GET"
    assert calls[0][2] is None


def test_readback_rejects_wrong_event_id() -> None:
    adapter = GoogleCalendarTestAdapter(calendar_id="test", transport=lambda *args: HttpResponse(200, {"id": "wrong"}), test_mode=True)
    with pytest.raises(GoogleCalendarError, match="identity mismatch"):
        adapter.get_event("requested")


@pytest.mark.parametrize("status,error", [(401,GoogleCalendarError),(403,GoogleCalendarError),(429,GoogleCalendarUnavailable),(503,GoogleCalendarUnavailable)])
def test_provider_errors_are_sanitized(status, error) -> None:
    adapter = GoogleCalendarTestAdapter(calendar_id="test", transport=lambda *args: HttpResponse(status, {"secret": "never leak"}), test_mode=True)
    with pytest.raises(error) as raised:
        adapter.get_event("event")
    assert "never leak" not in str(raised.value)


def test_fail_closed_no_writes_even_when_configured() -> None:
    calls = []
    adapter = GoogleCalendarTestAdapter(calendar_id="test", transport=lambda *args: calls.append(args), test_mode=True)
    now = datetime.now(UTC)
    with pytest.raises(GoogleCalendarError, match="writes disabled"):
        adapter.create_event(event_id="e", start_at=now, end_at=now+timedelta(minutes=30), expected_revision="v1")
    with pytest.raises(GoogleCalendarError):
        adapter.free_busy(from_at=now, until_at=now+timedelta(minutes=30))
    assert calls == []


@pytest.mark.parametrize("bad", ["../../other", "x/y", "", "test?send=true"])
def test_rejects_invalid_identifiers(bad) -> None:
    with pytest.raises(ValueError):
        GoogleCalendarTestAdapter(calendar_id=bad, transport=lambda *args: None, test_mode=True)


def test_pkce_request_contains_no_token_and_unique_state() -> None:
    a = build_google_test_authorization(client_id="client", redirect_uri="http://localhost:8000/oauth/callback")
    b = build_google_test_authorization(client_id="client", redirect_uri="http://localhost:8000/oauth/callback")
    params = parse_qs(urlparse(a.authorization_url).query)
    assert params["state"] == [a.state]
    assert params["code_challenge_method"] == ["S256"]
    assert "access_token" not in params
    assert a.state != b.state and a.code_verifier != b.code_verifier


def test_oauth_redirect_restrictions() -> None:
    with pytest.raises(ValueError):
        build_google_test_authorization(client_id="client", redirect_uri="http://remote.example/oauth")
