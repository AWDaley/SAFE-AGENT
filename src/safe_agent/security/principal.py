"""Trusted identity and signed, bounded local operator tokens.

No caller-supplied org/actor HTTP header establishes identity. Production identity
federation is a future integration; this service requires an external strong key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import field_validator

from safe_agent.canonical import canonical_json
from safe_agent.contracts.base import Identifier, StrictModel, require_aware

Scope = Literal["task:create", "task:prepare", "task:propose", "task:approve",
                "task:read", "task:audit"]


class AccessDenied(PermissionError):
    """Generic refusal: callers should not learn whether a foreign task exists."""


class Principal(StrictModel):
    organization_id: Identifier
    actor_id: Identifier
    scopes: tuple[Scope, ...]
    authenticated_until: datetime

    @field_validator("authenticated_until")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    def require(self, operation: Scope, organization_id: str, now: datetime) -> None:
        if (self.organization_id != organization_id or operation not in self.scopes
                or now >= self.authenticated_until):
            raise AccessDenied("operation not authorized")


class SignedOperatorTokens:
    """HMAC-verifiable test/local tokens, never minted by an unauthenticated endpoint.

    A real deployment must obtain principals from a managed identity provider;
    the signing key is a server-only secret and is never passed to the model.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("operator token signing key requires 32 random bytes")
        self._key = key

    def issue_for_trusted_operator(self, principal: Principal) -> str:
        payload = canonical_json({
            "type": "safe_agent.local_operator.v1",
            "aud": "safe-agent-local-api",
            "principal": principal.model_dump(mode="json"),
        })
        mac = hmac.new(self._key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + (
            base64.urlsafe_b64encode(mac).decode().rstrip("="))

    def verify(self, token: str, *, now: datetime) -> Principal:
        try:
            encoded, signature = token.split(".")
            if len(token) > 8192:
                raise ValueError("oversized token")
            data = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            mac = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            expected = hmac.new(self._key, data, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, mac):
                raise ValueError("invalid signature")
            obj = json.loads(data)
            if obj["type"] != "safe_agent.local_operator.v1" or obj["aud"] != "safe-agent-local-api":
                raise ValueError("invalid audience")
            principal = Principal.model_validate_json(json.dumps(obj["principal"]))
            if principal.authenticated_until <= require_aware(now).astimezone(UTC):
                raise ValueError("expired")
            return principal
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AccessDenied("invalid operator authorization") from error
