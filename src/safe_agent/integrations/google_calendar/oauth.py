"""OAuth2 PKCE for admin-created, explicitly isolated Google test calendars.

Never give refresh/access tokens, client secrets, calendar IDs or verifier bytes to
planners, their logs, the evidence ledger, or public task status endpoints.
Only a trusted local operator may begin and finish this OAuth transaction.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, delete
from sqlalchemy.orm import sessionmaker

from safe_agent.contracts.base import require_aware
from safe_agent.security.principal import Principal
from safe_agent.storage.models import GoogleOAuthPendingRow, GoogleTestConnectionRow

from .client import _validate_calendar_id
from .errors import GoogleCalendarError, GoogleConnectionDenied

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/calendar.events.owned",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
})


def _utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(UTC)


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("ascii")).hexdigest()


def _urlsafe(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class OAuthClientConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.redirect_uri)
        if (not self.client_id.strip() or not self.client_secret.strip()
                or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query
                or not parsed.path.startswith("/") or (parsed.scheme != "https"
                and not (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"))):
            raise ValueError("OAuth configuration requires an exact HTTPS/loopback redirect")


@dataclass(frozen=True)
class OAuthStart:
    authorize_url: str = field(repr=False)  # Trusted operator view ONLY, never log URL/state.
    state: str = field(repr=False)


@dataclass(frozen=True)
class TestConnection:
    connection_id: str
    organization_id: str
    actor_id: str
    calendar_id: str = field(repr=False)  # TRUSTED ONLY; never emit in model-visible results.
    refresh_token: str = field(repr=False)  # TRUSTED ONLY; never log.


class GoogleCredentialVault:
    """Encrypted SQL credentials and one-use OAuth state, scoped by operator."""

    def __init__(self, engine: Engine, *, fernet_key: bytes,
                 allowed_test_calendar_ids: tuple[str, ...]) -> None:
        self._cipher = Fernet(fernet_key)  # fail if key absent/invalid; no fallback
        if not allowed_test_calendar_ids:
            raise ValueError("an explicit test-calendar allowlist is required")
        self._allowlist = frozenset(
            _validate_calendar_id(calendar_id) for calendar_id in allowed_test_calendar_ids
        )
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def _allowed_id(self, calendar_id: str) -> str:
        calendar_id = _validate_calendar_id(calendar_id)
        if calendar_id not in self._allowlist:
            raise GoogleConnectionDenied("CALENDAR_NOT_ON_TEST_ALLOWLIST")
        return calendar_id

    def encrypt(self, value: str) -> bytes:
        return self._cipher.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        try:
            return self._cipher.decrypt(value).decode("utf-8")
        except (InvalidToken, UnicodeError) as error:
            raise GoogleConnectionDenied("CREDENTIAL_VAULT_UNAVAILABLE") from error

    def begin(self, *, principal: Principal, state: str, verifier: str,
              calendar_id: str, now: datetime) -> None:
        principal.require("connection:manage", principal.organization_id, now)
        with self._sessions.begin() as session:
            session.add(GoogleOAuthPendingRow(
                state_hash=_hash_state(state), organization_id=principal.organization_id,
                actor_id=principal.actor_id, verifier_ciphertext=self.encrypt(verifier),
                calendar_ciphertext=self.encrypt(self._allowed_id(calendar_id)),
                expires_at=_utc(now) + timedelta(minutes=10),
            ))

    def consume(self, *, principal: Principal, state: str,
                now: datetime) -> tuple[str, str]:
        principal.require("connection:manage", principal.organization_id, now)
        with self._sessions.begin() as session:
            # Claim and remove the exact scoped state in one database operation.
            # SELECT followed by an ORM delete can let two callbacks both read
            # the verifier and report success, even when one delete affects zero
            # rows. RETURNING releases values only to the transaction that won.
            row = session.execute(
                delete(GoogleOAuthPendingRow).where(
                    GoogleOAuthPendingRow.state_hash == _hash_state(state),
                    GoogleOAuthPendingRow.organization_id == principal.organization_id,
                    GoogleOAuthPendingRow.actor_id == principal.actor_id,
                    GoogleOAuthPendingRow.expires_at > _utc(now),
                ).returning(
                    GoogleOAuthPendingRow.verifier_ciphertext,
                    GoogleOAuthPendingRow.calendar_ciphertext,
                )
            ).one_or_none()
            if row is None:
                raise GoogleConnectionDenied("OAUTH_STATE_INVALID_OR_EXPIRED")
            verifier = self.decrypt(row.verifier_ciphertext)
            calendar_id = self._allowed_id(self.decrypt(row.calendar_ciphertext))
            # The context commits before these values reach the token exchange.
            # A failed commit never returns them; a later exchange failure cannot
            # resurrect the already-consumed state.
            return verifier, calendar_id

    def save(self, *, principal: Principal, refresh_token: str,
             calendar_id: str, now: datetime) -> str:
        principal.require("connection:manage", principal.organization_id, now)
        if len(refresh_token) < 8:
            raise GoogleConnectionDenied("GOOGLE_REFRESH_TOKEN_MISSING")
        connection_id = "gtest_" + secrets.token_hex(18)
        with self._sessions.begin() as session:
            session.add(GoogleTestConnectionRow(
                connection_id=connection_id, organization_id=principal.organization_id,
                actor_id=principal.actor_id, refresh_ciphertext=self.encrypt(refresh_token),
                calendar_ciphertext=self.encrypt(self._allowed_id(calendar_id)),
                scopes=" ".join(sorted(GOOGLE_SCOPES)), revoked=False, created_at=_utc(now),
            ))
        return connection_id

    def resolve(self, *, principal: Principal, connection_id: str,
                now: datetime) -> TestConnection:
        principal.require("connection:read", principal.organization_id, now)
        with self._sessions() as session:
            row = session.get(GoogleTestConnectionRow, connection_id)
            if (row is None or row.revoked or row.organization_id != principal.organization_id
                    or row.actor_id != principal.actor_id
                    or set(row.scopes.split()) != GOOGLE_SCOPES):
                raise GoogleConnectionDenied("GOOGLE_CONNECTION_NOT_AVAILABLE")
            return TestConnection(
                connection_id=row.connection_id, organization_id=row.organization_id,
                actor_id=row.actor_id, calendar_id=self._allowed_id(self.decrypt(row.calendar_ciphertext)),
                refresh_token=self.decrypt(row.refresh_ciphertext),
            )

    def rotate(self, *, principal: Principal, connection_id: str,
               new_refresh_token: str, now: datetime) -> None:
        principal.require("connection:read", principal.organization_id, now)
        if not new_refresh_token:
            raise GoogleConnectionDenied("GOOGLE_REFRESH_TOKEN_MISSING")
        with self._sessions.begin() as session:
            row = session.get(GoogleTestConnectionRow, connection_id, with_for_update=True)
            if (row is None or row.revoked or row.organization_id != principal.organization_id
                    or row.actor_id != principal.actor_id):
                raise GoogleConnectionDenied("GOOGLE_CONNECTION_NOT_AVAILABLE")
            row.refresh_ciphertext = self.encrypt(new_refresh_token)

    def revoke_local(self, *, principal: Principal, connection_id: str,
                     now: datetime) -> str:
        principal.require("connection:manage", principal.organization_id, now)
        # Fetch token only inside trusted procedure; erase in the same local transaction.
        with self._sessions.begin() as session:
            row = session.get(GoogleTestConnectionRow, connection_id, with_for_update=True)
            if (row is None or row.organization_id != principal.organization_id
                    or row.actor_id != principal.actor_id or row.revoked):
                raise GoogleConnectionDenied("GOOGLE_CONNECTION_NOT_AVAILABLE")
            token = self.decrypt(row.refresh_ciphertext)
            row.revoked = True
            row.revoked_at = _utc(now)
            row.refresh_ciphertext = self.encrypt("REVOKED")
            return token


class GoogleOAuthService:
    """Administrative OAuth only; no model-visible or unauthenticated setup APIs."""

    def __init__(self, vault: GoogleCredentialVault, config: OAuthClientConfig,
                 http: httpx.Client) -> None:
        self._vault, self._cfg, self._http = vault, config, http

    @property
    def redirect_uri(self) -> str:
        return self._cfg.redirect_uri

    def start(self, *, principal: Principal, calendar_id: str,
              now: datetime) -> OAuthStart:
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = _urlsafe(hashlib.sha256(verifier.encode("ascii")).digest())
        self._vault.begin(principal=principal, state=state, verifier=verifier,
                          calendar_id=calendar_id, now=now)
        params = {
            "client_id": self._cfg.client_id, "redirect_uri": self._cfg.redirect_uri,
            "response_type": "code", "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "include_granted_scopes": "false", "scope": " ".join(sorted(GOOGLE_SCOPES)),
        }
        return OAuthStart(AUTH_URL + "?" + urlencode(params), state)

    def _post_oauth(self, url: str, data: dict[str, str]) -> dict[str, object]:
        try:
            response = self._http.post(url, data=data, follow_redirects=False, timeout=10)
        except httpx.HTTPError as error:
            raise GoogleCalendarError("GOOGLE_OAUTH_NETWORK_ERROR") from error
        if response.status_code != 200:
            raise GoogleConnectionDenied("GOOGLE_OAUTH_TOKEN_REJECTED")
        try:
            payload = response.json()
        except ValueError as error:
            raise GoogleConnectionDenied("GOOGLE_OAUTH_MALFORMED") from error
        if not isinstance(payload, dict):
            raise GoogleConnectionDenied("GOOGLE_OAUTH_MALFORMED")
        return payload

    def complete(self, *, principal: Principal, state: str, code: str,
                 now: datetime) -> str:
        if (not isinstance(code, str) or not (8 <= len(code) <= 2048)
                or not isinstance(state, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{40,512}", state) is None):
            raise GoogleConnectionDenied("OAUTH_CALLBACK_INVALID")
        verifier, calendar_id = self._vault.consume(principal=principal, state=state, now=now)
        data = self._post_oauth(TOKEN_URL, {
            "grant_type": "authorization_code", "code": code,
            "client_id": self._cfg.client_id,
            "client_secret": self._cfg.client_secret,
            "redirect_uri": self._cfg.redirect_uri,
            "code_verifier": verifier,
        })
        if (set(str(data.get("scope", "")).split()) != GOOGLE_SCOPES
                or not isinstance(data.get("refresh_token"), str)):
            raise GoogleConnectionDenied("GOOGLE_SCOPES_OR_REFRESH_TOKEN_INVALID")
        return self._vault.save(principal=principal,
                                refresh_token=str(data["refresh_token"]),
                                calendar_id=calendar_id, now=now)

    def access_token(self, *, principal: Principal, connection_id: str,
                     now: datetime) -> tuple[str, str]:
        connection = self._vault.resolve(principal=principal, connection_id=connection_id,
                                         now=now)
        data = self._post_oauth(TOKEN_URL, {
            "grant_type": "refresh_token", "refresh_token": connection.refresh_token,
            "client_id": self._cfg.client_id, "client_secret": self._cfg.client_secret,
        })
        token = data.get("access_token")
        if not isinstance(token, str) or len(token) < 8:
            raise GoogleConnectionDenied("GOOGLE_ACCESS_TOKEN_MISSING")
        if "scope" in data and not GOOGLE_SCOPES.issubset(set(str(data["scope"]).split())):
            raise GoogleConnectionDenied("GOOGLE_ACCESS_TOKEN_SCOPES_INVALID")
        rotated = data.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            self._vault.rotate(principal=principal, connection_id=connection_id,
                               new_refresh_token=rotated, now=now)
        return token, connection.calendar_id

    def revoke(self, *, principal: Principal, connection_id: str,
               now: datetime) -> bool:
        """Local revocation always takes effect; remote revoke is best effort."""
        token = self._vault.revoke_local(principal=principal, connection_id=connection_id,
                                         now=now)
        try:
            response = self._http.post(REVOKE_URL, data={"token": token},
                                       follow_redirects=False, timeout=10)
            return response.status_code == 200
        except httpx.HTTPError:
            return False
