from __future__ import annotations

from datetime import timedelta

import pytest

from safe_agent.integrations.google_calendar.contracts import GoogleCalendarObservation
from safe_agent.integrations.google_calendar.errors import (
    GoogleCalendarError,
    GoogleConnectionDenied,
    GoogleTestWriteBlocked,
)
from tests.m5.conftest import CALENDAR, NOW, START, UNTIL
from tests.m5.test_google_http import INTENT


def _observe(kit, **overrides):
    kwargs = dict(
        principal=kit["principal"], connection_ref=kit["connection"],
        task_id="task_m5", calendar_ref="ref_calendar_m5",
        from_at=START, until_at=UNTIL, now=NOW,
    )
    kwargs.update(overrides)
    return kit["service"].observe(**kwargs)


def test_google_observation_is_sanitized_and_read_only(google_kit):
    kit = google_kit
    projection = _observe(kit)
    assert projection.provenance == "google_test_calendar"
    assert projection.advisory_only is True
    assert len(projection.free_slots) == 2
    assert projection.expires_at == NOW + timedelta(minutes=2)
    text = projection.model_dump_json()
    assert CALENDAR not in text
    assert "test_refresh_token" not in text
    assert "employee_demo" not in text
    assert kit["sim"].insert_calls == 0
    assert kit["repo"].replay_task("task_m5").external_effects == 0


def test_model_write_is_always_disabled(google_kit):
    kit = google_kit
    before = len(kit["sim"].calls)
    with pytest.raises(GoogleTestWriteBlocked, match="TAL_OFFICE_DOMAIN_NOT_CERTIFIED"):
        kit["service"].execute_model_proposal("forged_model_action")
    assert len(kit["sim"].calls) == before
    assert kit["service"].test_write_capability()["outbound_model_writes_enabled"] is False


def test_google_observation_cannot_use_forged_calendar_scope(google_kit):
    kit = google_kit
    with pytest.raises(GoogleCalendarError, match="TASK_SCOPE_NOT_AUTHORIZED"):
        _observe(kit, calendar_ref="ref_other_calendar")
    with pytest.raises(GoogleCalendarError):
        _observe(kit, task_id="task_m5", from_at=START - timedelta(hours=1))
    assert kit["sim"].auth_calls == 0


def test_google_observation_cross_actor_and_org_denied(google_kit):
    kit = google_kit
    other = kit["principal"].model_copy(update={"actor_id": "external_actor"})
    with pytest.raises(GoogleCalendarError):
        _observe(kit, principal=other)
    other_org = kit["principal"].model_copy(update={"organization_id": "other_org"})
    with pytest.raises(GoogleCalendarError):
        _observe(kit, principal=other_org)
    assert kit["sim"].auth_calls == 0


def test_test_connection_cannot_be_used_by_foreign_operator(google_kit):
    kit = google_kit
    foreign = kit["principal"].model_copy(update={"actor_id": "foreign"})
    with pytest.raises(GoogleConnectionDenied):
        kit["oauth"].access_token(principal=foreign, connection_id=kit["connection"], now=NOW)


def test_reconciliation_requires_verified_google_outbox(google_kit):
    kit = google_kit
    before = len(kit["sim"].calls)
    with pytest.raises(GoogleTestWriteBlocked, match="GOOGLE_OUTBOX_AND_TAL_GATE_NOT_CONFIGURED"):
        kit["service"].reconcile_test_effect(
            principal=kit["principal"], connection_ref=kit["connection"],
            intent=INTENT, task_id="task_m5", calendar_ref="ref_calendar_m5", now=NOW,
        )
    assert len(kit["sim"].calls) == before
    assert kit["repo"].replay_task("task_m5").external_effects == 0


def test_credential_disconnect_blocks_any_more_observations(google_kit):
    kit = google_kit
    assert _observe(kit).free_slots
    kit["oauth"].revoke(principal=kit["principal"], connection_id=kit["connection"], now=NOW)
    with pytest.raises(GoogleConnectionDenied):
        _observe(kit)


def test_google_test_projection_rejects_private_extra_fields(google_kit):
    from pydantic import ValidationError

    original = _observe(google_kit).model_dump()
    original["provider_calendar_id"] = CALENDAR
    with pytest.raises(ValidationError):
        GoogleCalendarObservation.model_validate(original)
