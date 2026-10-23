"""Real transaction checks for scoped, one-use OAuth state; no provider calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, select

from safe_agent.integrations.google_calendar.errors import GoogleConnectionDenied
from safe_agent.integrations.google_calendar.oauth import GoogleCredentialVault
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.models import GoogleOAuthPendingRow
from tests.m5.conftest import CALENDAR, NOW

DENIAL_CASES = (
    "tenant", "actor", "state", "expiry_boundary", "expired", "principal_expired", "scope",
)


def pending_state(engine):
    key = Fernet.generate_key()
    vaults = [
        GoogleCredentialVault(engine, fernet_key=key, allowed_test_calendar_ids=(CALENDAR,))
        for _ in range(2)
    ]
    principal = Principal(
        organization_id="org_oauth_atomic", actor_id="operator_oauth_atomic",
        scopes=("connection:manage",), authenticated_until=NOW + timedelta(days=1),
    )
    state = "oauth_state_" + uuid4().hex
    verifier = "synthetic_pkce_verifier_" + uuid4().hex
    vaults[0].begin(
        principal=principal, state=state, verifier=verifier, calendar_id=CALENDAR, now=NOW,
    )
    return vaults, principal, state, verifier


def assert_single_consumer(engine):
    vaults, principal, state, verifier = pending_state(engine)
    callers = Barrier(2)
    readers = Barrier(2)

    def overlap_old_read_window(_connection, _cursor, statement, *_):
        # Deterministically expose the former read-then-delete race if a
        # consumer reads this table before claiming the state. Atomic claims
        # need no preliminary read and are serialized by the database itself.
        if statement.lstrip().upper().startswith("SELECT") and "safe_google_oauth_pending" in statement:
            readers.wait(timeout=5)

    def consume(vault):
        callers.wait(timeout=5)
        try:
            return vault.consume(principal=principal, state=state, now=NOW)
        except GoogleConnectionDenied as error:
            assert str(error) == "OAUTH_STATE_INVALID_OR_EXPIRED"
            return None

    event.listen(engine, "after_cursor_execute", overlap_old_read_window)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, vaults))
    finally:
        event.remove(engine, "after_cursor_execute", overlap_old_read_window)
    assert results.count((verifier, CALENDAR)) == 1
    assert results.count(None) == 1
    with vaults[0]._sessions() as session:
        assert session.scalars(select(GoogleOAuthPendingRow)).all() == []
    with pytest.raises(GoogleConnectionDenied, match="OAUTH_STATE_INVALID_OR_EXPIRED"):
        vaults[1].consume(principal=principal, state=state, now=NOW)


def assert_scoped_state_denial(engine, case):
    vaults, principal, state, verifier = pending_state(engine)
    actor, presented_state, now = principal, state, NOW
    error_type = GoogleConnectionDenied
    if case == "tenant":
        actor = principal.model_copy(update={"organization_id": "org_foreign"})
    elif case == "actor":
        actor = principal.model_copy(update={"actor_id": "operator_foreign"})
    elif case == "state":
        presented_state += "missing"
    elif case == "expiry_boundary":
        now += timedelta(minutes=10)
    elif case == "expired":
        now += timedelta(minutes=10, microseconds=1)
    elif case == "principal_expired":
        actor = principal.model_copy(update={"authenticated_until": NOW})
        error_type = AccessDenied
    elif case == "scope":
        actor = principal.model_copy(update={"scopes": ("connection:read",)})
        error_type = AccessDenied
    else:
        raise AssertionError("unknown denial case")
    with pytest.raises(error_type):
        vaults[0].consume(principal=actor, state=presented_state, now=now)
    with vaults[0]._sessions() as session:
        assert len(session.scalars(select(GoogleOAuthPendingRow)).all()) == 1
    # Denial cannot steal a state from its rightful owner. The trusted fixture
    # clock is reset here only to inspect the original, still-unexpired state.
    assert vaults[1].consume(principal=principal, state=state, now=NOW) == (verifier, CALENDAR)


def assert_commit_failure_releases_no_state(engine):
    vaults, principal, state, verifier = pending_state(engine)

    def fail_commit(_connection):
        raise RuntimeError("authored OAuth commit failure")

    event.listen(engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="authored OAuth commit failure"):
            vaults[0].consume(principal=principal, state=state, now=NOW)
    finally:
        event.remove(engine, "commit", fail_commit)
    assert vaults[1].consume(principal=principal, state=state, now=NOW) == (verifier, CALENDAR)


def test_simultaneous_oauth_consumers_have_exactly_one_winner(google_kit):
    assert_single_consumer(google_kit["repo"].engine)
    assert google_kit["sim"].calls == []


@pytest.mark.parametrize("case", DENIAL_CASES)
def test_oauth_state_is_bound_to_scope_identity_and_expiry(google_kit, case):
    assert_scoped_state_denial(google_kit["repo"].engine, case)
    assert google_kit["sim"].calls == []


def test_oauth_commit_failure_cannot_release_verifier(google_kit):
    assert_commit_failure_releases_no_state(google_kit["repo"].engine)
    assert google_kit["sim"].calls == []
