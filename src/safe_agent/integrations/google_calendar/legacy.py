"""M5 foundation read-only adapter, retained as a legacy compatibility interface.

New integrations should use `GoogleCalendarHTTP` with `GoogleTestCalendarService`.
Both paths deny model-triggered real provider writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Mapping
from urllib.parse import quote


class GoogleCalendarError(RuntimeError):
    """Sanitized provider failure; never contains response bodies or tokens."""


class GoogleCalendarConflict(GoogleCalendarError):
    """Provider state no longer matches the expected state."""


class GoogleCalendarUnavailable(GoogleCalendarError):
    """Cannot establish an authoritative outcome."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: Mapping[str, object]


Transport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]
_ID = re.compile(r"^[A-Za-z0-9_@.\-]{1,256}$")


def _identifier(value: str) -> str:
    if not _ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError("invalid resource identifier")
    return quote(value, safe="")


def _instant(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class GoogleCalendarTestAdapter:
    """Legacy M5 read-only adapter with explicitly blocked effect dispatch.

    Retained for compatibility with the concurrently merged M5 foundation;
    it is never a model tool, TAL gate or source of write authorization.
    """

    def __init__(self, *, calendar_id: str, transport: Transport,
                 test_mode: bool = False) -> None:
        if not test_mode:
            raise ValueError("live calendar integration must be explicitly test-only")
        self._calendar_id = _identifier(calendar_id)
        self._transport = transport

    def _get(self, url: str) -> Mapping[str, object]:
        response = self._transport("GET", url, {"Accept": "application/json"}, None)
        if response.status in (401, 403):
            raise GoogleCalendarError("test calendar authorization denied")
        if response.status in (429, 500, 502, 503, 504):
            raise GoogleCalendarUnavailable("test calendar temporarily unavailable")
        if response.status == 404:
            raise GoogleCalendarError("test calendar resource absent")
        if response.status != 200:
            raise GoogleCalendarError("test calendar read rejected")
        return response.payload

    def free_busy(self, *, from_at: datetime, until_at: datetime) -> tuple[tuple[str, str], ...]:
        raise GoogleCalendarError("freeBusy connection not configured; fail closed")

    def get_event(self, event_id: str) -> Mapping[str, object]:
        url = ("https://www.googleapis.com/calendar/v3/calendars/"
               + self._calendar_id + "/events/" + _identifier(event_id))
        item = self._get(url)
        if item.get("id") != event_id:
            raise GoogleCalendarError("event identity mismatch")
        return {k: item[k] for k in ("id", "etag", "status", "start", "end") if k in item}

    def create_event(self, *, event_id: str, start_at: datetime,
                     end_at: datetime, expected_revision: str) -> None:
        _identifier(event_id)
        _instant(start_at)
        _instant(end_at)
        if not expected_revision:
            raise ValueError("expected revision required")
        raise GoogleCalendarError("external writes disabled pending upstream TAL office gate")
