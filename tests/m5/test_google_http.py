from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from safe_agent.integrations.google_calendar.client import GoogleCalendarHTTP
from safe_agent.integrations.google_calendar.contracts import (
    GoogleTestEventIntent,
    stable_event_id,
)
from safe_agent.integrations.google_calendar.errors import (
    GoogleConnectionDenied,
    GoogleRateLimited,
    GoogleStateConflict,
    GoogleUnknownOutcome,
)
from tests.m5.conftest import CALENDAR, START, UNTIL

INTENT = GoogleTestEventIntent(
    action_hash="sha256:" + "a" * 64,
    request_hash="sha256:" + "b" * 64,
    start_at=START, end_at=START + timedelta(minutes=30),
    time_zone="America/New_York",
)


def test_busy_returns_clipped_merged_availability_without_event_data(google_kit):
    sim = google_kit["sim"]
    sim.busy = [
        {"start": "2026-10-15T17:20:00Z", "end": "2026-10-15T18:00:00Z"},
        {"start": "2026-10-15T17:30:00Z", "end": "2026-10-15T18:15:00Z"},
        {"start": "2026-10-15T14:00:00Z", "end": "2026-10-15T16:00:00Z"},
    ]
    busy = google_kit["api"].busy_intervals(
        calendar_id=CALENDAR, token="token", start=START, end=UNTIL)
    assert len(busy) == 1
    assert busy[0][0] == datetime.fromisoformat("2026-10-15T17:20:00+00:00")
    assert busy[0][1] == datetime.fromisoformat("2026-10-15T18:15:00+00:00")


def test_calendar_find_effect_uses_deterministic_event_id(google_kit):
    kit = google_kit
    event_ref = kit["api"].insert_test_event(calendar_id=CALENDAR, token="token", intent=INTENT)
    assert event_ref == stable_event_id(INTENT.action_hash)
    assert event_ref.startswith("safe")
    assert len(event_ref) <= 64
    event = kit["api"].find_effect(calendar_id=CALENDAR, token="token", intent=INTENT)
    assert event is not None and event["id"] == event_ref
    assert kit["sim"].insert_calls == 1
    body = kit["sim"].events[event_ref]
    assert "attendees" not in body and "description" not in body
    assert body["visibility"] == "private"
    assert body["reminders"] == {"useDefault": False}


def test_timeout_after_insert_yields_unknown_and_reconciles_without_duplicate(google_kit):
    kit = google_kit
    kit["sim"].fail = "after_insert"
    with pytest.raises(GoogleUnknownOutcome):
        kit["api"].insert_test_event(calendar_id=CALENDAR, token="token", intent=INTENT)
    kit["sim"].fail = None
    event = kit["api"].find_effect(calendar_id=CALENDAR, token="token", intent=INTENT)
    assert event is not None
    assert kit["sim"].insert_calls == 1


def test_duplicate_id_or_different_payload_fails_closed(google_kit):
    kit = google_kit
    kit["api"].insert_test_event(calendar_id=CALENDAR, token="token", intent=INTENT)
    with pytest.raises(GoogleStateConflict):
        kit["api"].insert_test_event(calendar_id=CALENDAR, token="token", intent=INTENT)
    changed = INTENT.model_copy(update={"request_hash": "sha256:" + "c" * 64})
    with pytest.raises(GoogleStateConflict, match="GOOGLE_EFFECT_DOES_NOT_MATCH_INTENT"):
        kit["api"].find_effect(calendar_id=CALENDAR, token="token", intent=changed)


def test_get_missing_is_not_an_idempotency_permission(google_kit):
    kit = google_kit
    assert kit["api"].find_effect(calendar_id=CALENDAR, token="token", intent=INTENT) is None
    assert kit["sim"].insert_calls == 0


@pytest.mark.parametrize("failure,expected", [
    ("401", GoogleConnectionDenied),
    ("429", GoogleRateLimited),
])
def test_read_unavailable_fails_closed(google_kit, failure, expected):
    kit = google_kit
    kit["sim"].fail = failure
    with pytest.raises(expected):
        kit["api"].busy_intervals(calendar_id=CALENDAR, token="token", start=START, end=UNTIL)


def test_post_rate_limit_is_not_treated_as_success(google_kit):
    kit = google_kit
    kit["sim"].fail = "429"
    with pytest.raises(GoogleUnknownOutcome):
        kit["api"].insert_test_event(calendar_id=CALENDAR, token="token", intent=INTENT)
    assert kit["sim"].insert_calls == 0


def test_unknown_event_id_is_refused_before_network(google_kit):
    kit = google_kit
    original = len(kit["sim"].calls)
    with pytest.raises(ValueError):
        kit["api"].get_event(calendar_id=CALENDAR, event_id="../../bad", token="token")
    assert len(kit["sim"].calls) == original


def test_event_cancellation_and_missing_private_binding_are_rejected(google_kit):
    kit = google_kit
    event_ref = kit["api"].insert_test_event(calendar_id=CALENDAR, token="token", intent=INTENT)
    kit["sim"].events[event_ref]["extendedProperties"] = {}
    with pytest.raises(GoogleStateConflict):
        kit["api"].find_effect(calendar_id=CALENDAR, token="token", intent=INTENT)
    kit["sim"].events[event_ref]["status"] = "cancelled"
    with pytest.raises(GoogleStateConflict):
        kit["api"].find_effect(calendar_id=CALENDAR, token="token", intent=INTENT)


def test_no_calendar_notifications_or_private_contact_fields_in_insert(google_kit):
    kit = google_kit
    kit["api"].insert_test_event(calendar_id=CALENDAR, token="token", intent=INTENT)
    paths = [(method, path) for method, path in kit["sim"].calls if method == "POST"]
    assert paths == [("POST", "/calendar/v3/calendars/" + CALENDAR + "/events")]


def test_provider_client_cannot_insert_on_primary_calendar(google_kit):
    with pytest.raises(GoogleConnectionDenied):
        google_kit["api"].insert_test_event(calendar_id="primary", token="token", intent=INTENT)


def test_invalid_intent_times_and_unknown_fields_fail_closed():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GoogleTestEventIntent.model_validate({**INTENT.model_dump(), "access_token": "x"})
    with pytest.raises(ValidationError):
        GoogleTestEventIntent(action_hash=INTENT.action_hash, request_hash=INTENT.request_hash,
                              start_at=START, end_at=START + timedelta(minutes=90),
                              time_zone="America/New_York")


def test_invalid_calendar_timezone_or_offset_fails_before_provider_write():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GoogleTestEventIntent(
            action_hash=INTENT.action_hash, request_hash=INTENT.request_hash,
            start_at=INTENT.start_at, end_at=INTENT.end_at,
            time_zone="Imaginary/Land",
        )
    with pytest.raises(ValidationError):
        GoogleTestEventIntent(
            action_hash=INTENT.action_hash, request_hash=INTENT.request_hash,
            start_at=INTENT.start_at, end_at=INTENT.end_at,
            time_zone="Europe/London",  # Wrong UTC offset for New York instant.
        )
