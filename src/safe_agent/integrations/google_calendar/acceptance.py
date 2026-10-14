"""Trusted operator's read-only Google test-account acceptance preflight.

This deliberately does NOT certify M5 or authorize a live insertion. A passing
probe proves only a permission-filtered free/busy read at the time recorded.
Actual disposable Google account consent and end-to-end verified writes require
a separately approved manual acceptance run and independent security review.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
from pydantic import field_validator

from safe_agent.canonical import sha256
from safe_agent.contracts.base import Digest, StrictModel, require_aware
from safe_agent.security.principal import AccessDenied, Principal

from .errors import GoogleCalendarError
from .service import GoogleTestCalendarService


class GoogleReadOnlyPreflight(StrictModel):
    schema_version: Literal["safe_agent.google_read_only_preflight.v1"] = (
        "safe_agent.google_read_only_preflight.v1"
    )
    status: Literal["READ_ONLY_PASSED", "BLOCKED"]
    transport: Literal["mock", "configured_http", "none"]
    reason_code: Literal[
        "READ_ONLY_OBSERVED", "NO_TRUSTED_CONNECTION",
        "READ_SCOPE_DENIED", "PROVIDER_READ_UNAVAILABLE",
    ]
    checked_at: datetime
    task_digest: Digest
    observation_digest: Digest | None = None
    test_calendar_allowlist_enforced: Literal[True] = True
    provider_write_performed: Literal[False] = False
    provider_write_verified: Literal[False] = False
    m5_live_acceptance: Literal["BLOCKED"] = "BLOCKED"
    production_authorized: Literal[False] = False

    @field_validator("checked_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class GoogleTestAcceptancePreflight:
    """Trusted read-only service, never installed as an agent or HTTP action tool.

    If a real HTTP transport is supplied by the operator backend, the probe
    may query the allowlisted calendar using existing OAuth credentials.
    No calendar writes, arbitrary URLs, approval grants or task state updates.
    A probe result never marks a live-account test accepted.
    """

    def __init__(self, service: GoogleTestCalendarService | None) -> None:
        if service is not None and type(service) is not GoogleTestCalendarService:
            raise TypeError("GOOGLE_PREFLIGHT_REQUIRES_TRUSTED_TEST_SERVICE")
        self._service = service

    def run(
        self, *, principal: Principal, task_id: str,
        connection_ref: str, calendar_ref: str,
        from_at: datetime, until_at: datetime, now: datetime,
    ) -> GoogleReadOnlyPreflight:
        now = require_aware(now)
        from_at, until_at = require_aware(from_at), require_aware(until_at)
        if not task_id or not connection_ref or not calendar_ref or from_at >= until_at:
            raise ValueError("invalid bounded test-account preflight request")
        scope_hash = sha256({
            "organization_id": principal.organization_id,
            "task_id": task_id, "calendar_ref": calendar_ref,
            "connection_ref": connection_ref,
            "from_at": from_at, "until_at": until_at,
        })
        source = self._service
        transport: Literal["mock", "configured_http", "none"] = "none"
        if source is not None:
            http = source._api._http
            oauth_http = source._oauth._http
            transport = (
                "mock" if type(http._transport) is httpx.MockTransport
                and type(oauth_http._transport) is httpx.MockTransport
                else "configured_http"
            )

        def blocked(code: Literal[
            "NO_TRUSTED_CONNECTION", "READ_SCOPE_DENIED", "PROVIDER_READ_UNAVAILABLE",
        ]) -> GoogleReadOnlyPreflight:
            return GoogleReadOnlyPreflight(
                status="BLOCKED", transport=transport, reason_code=code,
                checked_at=now, task_digest=scope_hash,
            )

        try:
            principal.require("task:read", principal.organization_id, now)
            principal.require("connection:read", principal.organization_id, now)
        except AccessDenied:
            return blocked("READ_SCOPE_DENIED")
        if source is None:
            return blocked("NO_TRUSTED_CONNECTION")
        # This must stay a separate, non-executable observation; the provider
        # service independently validates manifest/tenant/actor/OAuth allowlist.
        try:
            observation = source.observe(
                principal=principal, connection_ref=connection_ref,
                task_id=task_id, calendar_ref=calendar_ref,
                from_at=from_at, until_at=until_at, now=now,
            )
        except (AccessDenied, GoogleCalendarError, ValueError):
            return blocked("PROVIDER_READ_UNAVAILABLE")
        return GoogleReadOnlyPreflight(
            status="READ_ONLY_PASSED", transport=transport,
            reason_code="READ_ONLY_OBSERVED", checked_at=now,
            task_digest=scope_hash,
            observation_digest=sha256(observation),
        )
