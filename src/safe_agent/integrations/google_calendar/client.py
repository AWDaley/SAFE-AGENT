"""Narrow Google Calendar REST client for an explicit isolated test calendar.

All URL origins are fixed, HTTPS-only, redirects forbidden; no model-facing tools.
The low-level insert method is trusted infrastructure and is never wired to M4
SyntheticRecoveryWorker. Caller must independently enforce TAL admission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from safe_agent.contracts.base import require_aware

from .contracts import GoogleTestEventIntent, stable_event_id
from .errors import (
    GoogleCalendarError,
    GoogleConnectionDenied,
    GoogleRateLimited,
    GoogleStateConflict,
    GoogleUnknownOutcome,
)

GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"


def _rfc3339(value: datetime) -> str:
    return require_aware(value).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GoogleCalendarError("INVALID_PROVIDER_DATETIME")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return require_aware(result).astimezone(UTC)
    except ValueError as error:
        raise GoogleCalendarError("INVALID_PROVIDER_DATETIME") from error


def _validate_calendar_id(calendar_id: str) -> str:
    if not isinstance(calendar_id, str) or not (3 <= len(calendar_id) <= 256):
        raise GoogleConnectionDenied("TEST_CALENDAR_ID_INVALID")
    if calendar_id in {"primary", "", "default"} or any(
        char in calendar_id for char in "\\/\r\n\x00?#"
    ):
        raise GoogleConnectionDenied("EXPLICIT_TEST_CALENDAR_REQUIRED")
    return calendar_id


class GoogleCalendarHTTP:
    """Trusted, provider-specific HTTP surface; mockable without network."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def _request(self, method: str, path: str, *, token: str,
                 params: dict[str, str] | None = None,
                 payload: dict[str, object] | None = None,
                 write: bool = False) -> dict[str, Any] | None:
        if method not in {"GET", "POST"} or not path.startswith("/"):
            raise ValueError("unsupported Google REST operation")
        try:
            response = self._http.request(
                method, GOOGLE_CALENDAR_API + path,
                params=params, json=payload,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                follow_redirects=False, timeout=10.0,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            code = "GOOGLE_EFFECT_OUTCOME_UNKNOWN" if write else "GOOGLE_READ_UNAVAILABLE"
            raise GoogleUnknownOutcome(code) from error
        if response.status_code == 404 and method == "GET":
            return None
        if response.status_code in {401, 403}:
            raise GoogleConnectionDenied("GOOGLE_PERMISSION_OR_TOKEN_DENIED")
        if response.status_code in {429, 500, 502, 503, 504}:
            if write:
                raise GoogleUnknownOutcome("GOOGLE_EFFECT_OUTCOME_UNKNOWN")
            raise GoogleRateLimited("GOOGLE_UNAVAILABLE_OR_RATE_LIMITED")
        if response.status_code == 409:
            raise GoogleStateConflict("GOOGLE_EVENT_ID_CONFLICT")
        if not 200 <= response.status_code < 300:
            # Never include Google's response body; it can contain private data.
            raise GoogleCalendarError("GOOGLE_REQUEST_REJECTED")
        try:
            data = response.json()
        except ValueError as error:
            raise GoogleUnknownOutcome("GOOGLE_UNPARSABLE_RESPONSE") from error
        if not isinstance(data, dict):
            raise GoogleUnknownOutcome("GOOGLE_UNPARSABLE_RESPONSE")
        return data

    def busy_intervals(self, *, calendar_id: str, token: str,
                       start: datetime, end: datetime) -> tuple[tuple[datetime, datetime], ...]:
        cid = _validate_calendar_id(calendar_id)
        start, end = require_aware(start), require_aware(end)
        if (start >= end or (end - start).total_seconds() > 14 * 86400):
            raise ValueError("calendar query range invalid")
        response = self._request(
            "POST", "/freeBusy", token=token,
            payload={"timeMin": _rfc3339(start), "timeMax": _rfc3339(end),
                     "items": [{"id": cid}], "timeZone": "UTC"},
        )
        if response is None:
            raise GoogleCalendarError("GOOGLE_FREEBUSY_NOT_FOUND")
        calendars = response.get("calendars")
        if not isinstance(calendars, dict):
            raise GoogleCalendarError("GOOGLE_FREEBUSY_MALFORMED")
        record = calendars.get(cid)
        if not isinstance(record, dict) or record.get("errors"):
            raise GoogleConnectionDenied("GOOGLE_CALENDAR_ACCESS_UNAVAILABLE")
        entries = record.get("busy")
        if not isinstance(entries, list):
            raise GoogleCalendarError("GOOGLE_FREEBUSY_MALFORMED")
        result: list[tuple[datetime, datetime]] = []
        for item in entries:
            if not isinstance(item, dict):
                raise GoogleCalendarError("GOOGLE_FREEBUSY_MALFORMED")
            a, b = _parse_rfc3339(item.get("start")), _parse_rfc3339(item.get("end"))
            if a >= b:
                raise GoogleCalendarError("GOOGLE_FREEBUSY_MALFORMED")
            lo, hi = max(a, start.astimezone(UTC)), min(b, end.astimezone(UTC))
            if lo < hi:
                result.append((lo, hi))
        # Merge overlaps, including repeated events; do not expose event metadata.
        merged: list[tuple[datetime, datetime]] = []
        for a, b in sorted(result):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return tuple(merged)

    def get_event(self, *, calendar_id: str, event_id: str,
                  token: str) -> dict[str, Any] | None:
        cid = _validate_calendar_id(calendar_id)
        if not event_id.startswith("safe") or len(event_id) > 64 or not all(
            x in "0123456789abcdefghijklmnopqrstuv" for x in event_id
        ):
            raise ValueError("untrusted event ID")
        return self._request(
            "GET", f"/calendars/{quote(cid, safe='')}/events/{event_id}", token=token,
            params={"fields": "id,status,etag,start,end,extendedProperties"},
        )

    def find_effect(self, *, calendar_id: str, token: str,
                    intent: GoogleTestEventIntent) -> dict[str, Any] | None:
        event_id = stable_event_id(intent.action_hash)
        event = self.get_event(calendar_id=calendar_id, event_id=event_id, token=token)
        if event is None:
            return None
        extended = event.get("extendedProperties")
        private = extended.get("private") if isinstance(extended, dict) else None
        start = event.get("start", {})
        end = event.get("end", {})
        if (event.get("id") != event_id or event.get("status") != "confirmed"
                or not isinstance(private, dict)
                or private.get("safe_agent_action") != intent.action_hash
                or private.get("safe_agent_request") != intent.request_hash
                or not isinstance(start, dict) or not isinstance(end, dict)
                or _parse_rfc3339(start.get("dateTime")) != intent.start_at.astimezone(UTC)
                or _parse_rfc3339(end.get("dateTime")) != intent.end_at.astimezone(UTC)):
            raise GoogleStateConflict("GOOGLE_EFFECT_DOES_NOT_MATCH_INTENT")
        return event

    def insert_test_event(self, *, calendar_id: str, token: str,
                          intent: GoogleTestEventIntent) -> str:
        """Trusted low-level operation; does NOT confer authorization to call it.

        A timeout might have committed: the caller must reconcile the deterministic
        event ID before any new attempt, never blindly retry a POST.
        """
        cid = _validate_calendar_id(calendar_id)
        body: dict[str, object] = {
            "id": stable_event_id(intent.action_hash),
            "summary": "SAFE-AGENT isolated calendar test",
            "visibility": "private", "transparency": "opaque",
            "guestsCanInviteOthers": False, "guestsCanModify": False,
            "reminders": {"useDefault": False},
            "start": {"dateTime": intent.start_at.isoformat(), "timeZone": intent.time_zone},
            "end": {"dateTime": intent.end_at.isoformat(), "timeZone": intent.time_zone},
            "extendedProperties": {"private": {
                "safe_agent_action": intent.action_hash,
                "safe_agent_request": intent.request_hash,
            }},
        }
        result = self._request(
            "POST", f"/calendars/{quote(cid, safe='')}/events", token=token,
            params={"sendUpdates": "none"}, payload=body, write=True,
        )
        if result is None or result.get("id") != stable_event_id(intent.action_hash):
            raise GoogleUnknownOutcome("GOOGLE_INSERT_RESPONSE_UNVERIFIED")
        return stable_event_id(intent.action_hash)
