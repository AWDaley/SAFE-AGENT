from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from safe_agent.integrations.google_calendar.errors import GoogleConnectionDenied
from safe_agent.integrations.google_calendar.oauth import (
    GOOGLE_SCOPES, GoogleCredentialVault, OAuthClientConfig,
)
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.models import GoogleOAuthPendingRow, GoogleTestConnectionRow
from tests.m5.conftest import CALENDAR, NOW


def test_oauth_authorization_url_has_pkce_state_exact_scopes(google_kit):
    start = google_kit["oauth"].start(principal=google_kit["principal"],
                                      calendar_id=CALENDAR, now=NOW)
    parsed = urlparse(start.authorize_url)
    q = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.netloc == "accounts.google.com"
    assert q["state"] == [start.state]
    assert q["code_challenge_method"] == ["S256"]
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert set(q["scope"][0].split()) == GOOGLE_SCOPES
    assert "test_client_secret" not in start.authorize_url
    with google_kit["repo"]._sessions() as session:
        pending = session.scalars(select(GoogleOAuthPendingRow)).one()
        assert start.state not in pending.state_hash
        assert b"test" not in pending.calendar_ciphertext


def test_oauth_complete_stores_encrypted_refresh_only_and_one_use_state(google_kit):
    kit = google_kit
    start = kit["oauth"].start(principal=kit["principal"], calendar_id=CALENDAR, now=NOW)
    connection = kit["oauth"].complete(principal=kit["principal"],
                                       state=start.state, code="authorization_code_abc",
                                       now=NOW + timedelta(seconds=2))
    assert connection.startswith("gtest_")
    with kit["repo"]._sessions() as session:
        row = session.get(GoogleTestConnectionRow, connection)
        assert row is not None and not row.revoked
        assert b"test_refresh_token_abcdef" not in row.refresh_ciphertext
        assert CALENDAR.encode() not in row.calendar_ciphertext
        assert session.scalars(select(GoogleOAuthPendingRow)).all() == []
    with pytest.raises(GoogleConnectionDenied):
        kit["oauth"].complete(principal=kit["principal"],
                              state=start.state, code="authorization_code_abc", now=NOW)


def test_oauth_scope_expansion_fails_and_consumes_state(google_kit):
    kit = google_kit
    start = kit["oauth"].start(principal=kit["principal"], calendar_id=CALENDAR, now=NOW)
    kit["sim"].scopes += " https://www.googleapis.com/auth/calendar"
    with pytest.raises(GoogleConnectionDenied, match="GOOGLE_SCOPES_OR_REFRESH_TOKEN_INVALID"):
        kit["oauth"].complete(principal=kit["principal"],
                              state=start.state, code="authorization_code_abc", now=NOW)
    with pytest.raises(GoogleConnectionDenied):
        kit["oauth"].complete(principal=kit["principal"],
                              state=start.state, code="authorization_code_abc", now=NOW)


def test_oauth_state_tenant_actor_expiry_fail_closed(google_kit):
    kit = google_kit
    start = kit["oauth"].start(principal=kit["principal"], calendar_id=CALENDAR, now=NOW)
    foreign = kit["principal"].model_copy(update={"actor_id": "intruder"})
    with pytest.raises(GoogleConnectionDenied):
        kit["oauth"].complete(principal=foreign, state=start.state,
                              code="authorization_code_abc", now=NOW)
    with pytest.raises(GoogleConnectionDenied):
        kit["oauth"].complete(principal=kit["principal"], state=start.state,
                              code="authorization_code_abc", now=NOW + timedelta(minutes=11))


def test_oauth_untrusted_redirect_or_primary_calendar_refused(google_kit):
    from safe_agent.integrations.google_calendar.client import _validate_calendar_id

    for redirect in ("http://example.com/callback", "https://a.example/cb?x=1",
                     "https://user:pwd@example.com/cb", "javascript:/oops"):
        with pytest.raises(ValueError):
            OAuthClientConfig("client", "secret", redirect)
    for cid in ("primary", "default", "a/../../../primary", "\ncal"):
        with pytest.raises(GoogleConnectionDenied):
            _validate_calendar_id(cid)


def test_oauth_token_refresh_and_rotated_secret(google_kit):
    kit = google_kit
    token, private_calendar = kit["oauth"].access_token(
        principal=kit["principal"], connection_id=kit["connection"], now=NOW)
    assert token == "test_access_token_abc123"
    assert private_calendar == CALENDAR
    kit["vault"].rotate(principal=kit["principal"], connection_id=kit["connection"],
                        new_refresh_token="new_test_refresh_token_abcdef", now=NOW)
    resolved = kit["vault"].resolve(principal=kit["principal"],
                                    connection_id=kit["connection"], now=NOW)
    assert resolved.refresh_token == "new_test_refresh_token_abcdef"


def test_oauth_revoke_is_immediate_even_if_remote_fails(google_kit):
    kit = google_kit
    kit["sim"].fail = "revoke"
    assert kit["oauth"].revoke(principal=kit["principal"],
                               connection_id=kit["connection"], now=NOW) is False
    with pytest.raises(GoogleConnectionDenied):
        kit["oauth"].access_token(principal=kit["principal"],
                                  connection_id=kit["connection"], now=NOW)


def test_oauth_denied_without_manage_scope_or_read_scope(google_kit):
    kit = google_kit
    unauth = Principal(
        organization_id="org_demo", actor_id="employee_demo", scopes=("task:read",),
        authenticated_until=NOW + timedelta(days=1),
    )
    with pytest.raises(AccessDenied):
        kit["oauth"].start(principal=unauth, calendar_id=CALENDAR, now=NOW)
    with pytest.raises(AccessDenied):
        kit["vault"].resolve(principal=unauth,
                             connection_id=kit["connection"], now=NOW)


def test_vault_key_loss_fails_closed_after_restart(google_kit):
    kit = google_kit
    other = GoogleCredentialVault(kit["repo"].engine, fernet_key=Fernet.generate_key(),
                                   allowed_test_calendar_ids=(CALENDAR,))
    with pytest.raises(GoogleConnectionDenied, match="CREDENTIAL_VAULT_UNAVAILABLE"):
        other.resolve(principal=kit["principal"], connection_id=kit["connection"], now=NOW)


def test_oauth_error_never_prints_provider_token_or_body(google_kit):
    kit = google_kit
    kit["sim"].fail = "oauth"
    with pytest.raises(GoogleConnectionDenied) as exc:
        kit["oauth"].access_token(principal=kit["principal"],
                                  connection_id=kit["connection"], now=NOW)
    assert "test_refresh_token" not in str(exc.value)
    assert "sensitive" not in str(exc.value)


def test_calendar_allowlist_cannot_be_empty_or_bypassed(google_kit):
    kit = google_kit
    with pytest.raises(ValueError, match="allowlist"):
        GoogleCredentialVault(kit["repo"].engine, fernet_key=Fernet.generate_key(),
                              allowed_test_calendar_ids=())
    with pytest.raises(GoogleConnectionDenied, match="CALENDAR_NOT_ON_TEST_ALLOWLIST"):
        kit["oauth"].start(principal=kit["principal"],
                           calendar_id="someone@group.calendar.google.com", now=NOW)
    with pytest.raises(GoogleConnectionDenied, match="CALENDAR_NOT_ON_TEST_ALLOWLIST"):
        kit["vault"].save(principal=kit["principal"],
                          refresh_token="test_refresh_token", calendar_id="other@example.com",
                          now=NOW)


def test_calendar_removed_from_allowlist_cannot_be_loaded(google_kit):
    kit = google_kit
    restricted = GoogleCredentialVault(
        kit["repo"].engine, fernet_key=Fernet.generate_key(),
        allowed_test_calendar_ids=("different@group.calendar.google.com",),
    )
    # A rotated or missing key fails closed before returning any stored provider ID.
    with pytest.raises(GoogleConnectionDenied):
        restricted.resolve(principal=kit["principal"],
                           connection_id=kit["connection"], now=NOW)
