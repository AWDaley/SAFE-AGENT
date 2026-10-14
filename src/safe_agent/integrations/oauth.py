"""Legacy M5 OAuth request-only helper; use google_calendar.oauth for trusted flows.

Compatibility builder from M5 foundation. It is not an authenticated connection
and cannot exchange or retain tokens. Do not expose its PKCE verifier to models.
"""
from __future__ import annotations

import hashlib
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from safe_agent.integrations.google_calendar.oauth import GOOGLE_SCOPES


@dataclass(frozen=True)
class OAuthRequest:
    authorization_url: str
    state: str
    code_verifier: str


def build_google_test_authorization(*, client_id: str, redirect_uri: str) -> OAuthRequest:
    """Build PKCE request only; does not persist/verify OAuth state."""
    target = urlparse(redirect_uri)
    if target.scheme != "https" and not (
        target.scheme == "http" and target.hostname in ("localhost", "127.0.0.1")
    ):
        raise ValueError("OAuth redirect must be HTTPS or localhost")
    if not target.netloc or target.username or target.password or not client_id.strip():
        raise ValueError("invalid OAuth client configuration")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(
        b"="
    ).decode("ascii")
    query = urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": " ".join(sorted(GOOGLE_SCOPES)),
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent",
    })
    return OAuthRequest("https://accounts.google.com/o/oauth2/v2/auth?" + query, state, verifier)
