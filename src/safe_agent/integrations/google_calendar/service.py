"""Read-only Google Calendar test connection within the existing agent trust boundaries.

M5 provider adapter supports mocked insert and deterministic read-back, but the
SAFE-AGENT coordinator/worker MUST NOT route live model actions here until a
verified TAL office-domain action gate exists. Google supplies no atomic booking
transaction; free/busy previews are advisory and cannot guarantee slot ownership.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safe_agent.canonical import sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.storage.repository import LedgerRepository
from safe_agent.contracts.office import AvailableSlot
from safe_agent.security.principal import Principal

from .client import GoogleCalendarHTTP
from .contracts import GoogleTestEventIntent, GoogleCalendarObservation
from .errors import GoogleCalendarError, GoogleTestWriteBlocked
from .oauth import GoogleOAuthService


class GoogleTestCalendarService:
    def __init__(self, oauth: GoogleOAuthService, api: GoogleCalendarHTTP,
                 repo: LedgerRepository) -> None:
        self._oauth = oauth
        self._api = api
        self._repo = repo

    def _authorize_observation(
        self, principal: Principal, task_id: str, calendar_ref: str,
        from_at: datetime, until_at: datetime, now: datetime,
    ) -> None:
        principal.require("task:read", principal.organization_id, now)
        with self._repo.write_task(task_id) as tx:
            task, manifest = tx.task, tx.manifest
            if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
                    or task.organization_id != principal.organization_id
                    or task.initiator_id != principal.actor_id
                    or manifest.calendar_ref != calendar_ref
                    or manifest.organization_id != principal.organization_id
                    or manifest.task_id != task_id
                    or manifest.initiator_id != principal.actor_id
                    or "calendar.create_event" not in manifest.permitted_operations
                    or from_at < manifest.allowed_start or until_at > manifest.allowed_end
                    or now >= task.deadline):
                raise GoogleCalendarError("TASK_SCOPE_NOT_AUTHORIZED")

    def observe(
        self, *, principal: Principal, connection_ref: str,
        task_id: str, calendar_ref: str,
        from_at: datetime, until_at: datetime, now: datetime,
    ) -> GoogleCalendarObservation:
        """Fetch only free/busy. Provider ID/contents stay outside model context."""
        now = require_aware(now)
        from_at, until_at = require_aware(from_at), require_aware(until_at)
        principal.require("connection:read", principal.organization_id, now)
        if not task_id or not calendar_ref or from_at >= until_at:
            raise ValueError("invalid sanitized observation input")
        self._authorize_observation(
            principal, task_id, calendar_ref, from_at, until_at, now,
        )
        token, private_id = self._oauth.access_token(
            principal=principal, connection_id=connection_ref, now=now,
        )
        busy = self._api.busy_intervals(
            calendar_id=private_id, token=token, start=from_at, end=until_at,
        )
        cursor = from_at.astimezone(UTC)
        upper = until_at.astimezone(UTC)
        free: list[AvailableSlot] = []
        for a, b in busy:
            if cursor < a:
                free.append(AvailableSlot(start_at=cursor, end_at=a))
            cursor = max(cursor, b)
        if cursor < upper:
            free.append(AvailableSlot(start_at=cursor, end_at=upper))
        fetched = now.astimezone(UTC)
        expire = fetched + timedelta(minutes=2)
        digest = sha256({
            "organization_id": principal.organization_id,
            "task_id": task_id, "calendar_ref": calendar_ref,
            "connection_ref": connection_ref, "free_slots": free,
            "from_at": from_at, "until_at": until_at,
            "fetched_at": fetched, "expires_at": expire,
        })
        return GoogleCalendarObservation(
            organization_id=principal.organization_id, task_id=task_id,
            calendar_ref=calendar_ref, connection_ref=connection_ref,
            revision=digest, from_at=from_at, until_at=until_at,
            fetched_at=fetched, expires_at=expire, free_slots=tuple(free),
        )

    def reconcile_test_effect(
        self, *, principal: Principal, connection_ref: str,
        intent: GoogleTestEventIntent, task_id: str, calendar_ref: str,
        now: datetime,
    ) -> bool | None:
        """Fail closed until TAL/outbox binds this action to a Google connection.

        The trusted low-level `GoogleCalendarHTTP.find_effect` is exercised in
        simulated provider contract tests, not available as an arbitrary user
        probe. Task-scoped Google action records do not exist in M5.
        """
        raise GoogleTestWriteBlocked("GOOGLE_OUTBOX_AND_TAL_GATE_NOT_CONFIGURED")

    def execute_model_proposal(self, *args: object, **kwargs: object) -> None:
        """No second authority path for Google calendar writes in M5.

        Models have no access to this service or OAuth credentials. The only
        future route is the certified TAL calendar-domain action gate + the M4
        durable outbox, tested with an isolated provider account.
        """
        raise GoogleTestWriteBlocked("TAL_OFFICE_DOMAIN_NOT_CERTIFIED")

    def test_write_capability(self) -> dict[str, object]:
        """Useful fail-closed operator diagnostics without secret or provider IDs."""
        return {
            "provider": "google_calendar", "environment": "isolated_test_only",
            "can_observe_freebusy": True,
            "provider_insert_implemented": True,
            "outbound_model_writes_enabled": False,
            "reason": "TAL_OFFICE_DOMAIN_NOT_CERTIFIED",
        }
