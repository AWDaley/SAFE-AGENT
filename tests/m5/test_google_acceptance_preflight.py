"""PR4 read-only operator preflight and provider-sourced simulation contracts."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.integrations.google_calendar.acceptance import (
    GoogleReadOnlyPreflight,
    GoogleTestAcceptancePreflight,
)
from safe_agent.integrations.google_calendar.provider_simulation import (
    GoogleProviderSimulationInvalid,
    GoogleTestProviderSimulation,
    simulate_provider_proposal,
)
from tests.m5.conftest import CALENDAR, NOW, START


def _proposal():
    return CalendarCreateEvent(
        calendar_ref="ref_calendar_m5", customer_ref="ref_customer_m5",
        template_id="follow_up_v1",
        start_at=START, end_at=START + timedelta(minutes=30),
        time_zone="America/New_York",
        projection_rev="projection_test_only",
        simulation_ref="simulation_test_only",
    )


def _observe(kit, *, now=NOW):
    return kit["service"].observe(
        principal=kit["principal"], connection_ref=kit["connection"],
        task_id="task_m5", calendar_ref="ref_calendar_m5",
        from_at=START, until_at=START + timedelta(minutes=30),
        now=now,
    )


def _preflight(kit, *, principal=None):
    return GoogleTestAcceptancePreflight(kit["service"]).run(
        principal=principal or kit["principal"],
        task_id="task_m5", connection_ref=kit["connection"],
        calendar_ref="ref_calendar_m5",
        from_at=START, until_at=START + timedelta(minutes=30),
        now=NOW,
    )


def test_provider_simulation_pins_observation_and_proposal_without_holding_slot(google_kit):
    observation = _observe(google_kit)
    result = simulate_provider_proposal(
        observation=observation, proposal=_proposal(),
        manifest=google_kit["manifest"], now=NOW,
    )
    result.verify_hash()
    assert result.provenance == "google_test_calendar"
    assert result.advisory_only is True
    assert result.holds_slot is False
    assert result.proposed_slot_available is True
    assert result.observation_revision == observation.revision
    assert result.model_dump_json().find(CALENDAR) == -1
    assert result == simulate_provider_proposal(
        observation=observation, proposal=_proposal(),
        manifest=google_kit["manifest"], now=NOW,
    )
    assert google_kit["sim"].insert_calls == 0


def test_modified_provider_observation_or_proposal_refused(google_kit):
    original = _observe(google_kit)
    forged = original.model_copy(update={"revision": "sha256:" + "0" * 64})
    with pytest.raises(GoogleProviderSimulationInvalid):
        simulate_provider_proposal(
            observation=forged, proposal=_proposal(),
            manifest=google_kit["manifest"], now=NOW,
        )
    different = _proposal().model_copy(update={"customer_ref": "ref_foreign"})
    with pytest.raises(GoogleProviderSimulationInvalid):
        simulate_provider_proposal(
            observation=original, proposal=different,
            manifest=google_kit["manifest"], now=NOW,
        )
    altered_slot = original.model_copy(update={"free_slots": ()})
    with pytest.raises(GoogleProviderSimulationInvalid):
        simulate_provider_proposal(
            observation=altered_slot, proposal=_proposal(),
            manifest=google_kit["manifest"], now=NOW,
        )
    assert google_kit["sim"].insert_calls == 0


def test_expired_provider_observation_or_mutated_simulation_rejected(google_kit):
    original = _observe(google_kit)
    with pytest.raises(GoogleProviderSimulationInvalid):
        simulate_provider_proposal(
            observation=original, proposal=_proposal(),
            manifest=google_kit["manifest"], now=NOW + timedelta(minutes=3),
        )
    sim = simulate_provider_proposal(
        observation=original, proposal=_proposal(),
        manifest=google_kit["manifest"], now=NOW,
    )
    altered = sim.model_copy(update={"observation_hash": "sha256:" + "1" * 64})
    with pytest.raises(GoogleProviderSimulationInvalid):
        altered.verify_hash()
    with pytest.raises(ValidationError):
        GoogleTestProviderSimulation.model_validate({
            **sim.model_dump(), "calendar_id": CALENDAR,
        })


def test_read_only_operator_preflight_does_not_claim_m5_live_acceptance(google_kit):
    report = _preflight(google_kit)
    assert report.status == "READ_ONLY_PASSED"
    assert report.reason_code == "READ_ONLY_OBSERVED"
    assert report.transport == "mock"
    assert report.m5_live_acceptance == "BLOCKED"
    assert report.provider_write_verified is False
    assert report.provider_write_performed is False
    assert report.production_authorized is False
    assert report.observation_digest is not None
    safe_json = report.model_dump_json()
    assert CALENDAR not in safe_json
    assert "refresh_token" not in safe_json
    assert "test_access_token" not in safe_json
    assert "employee_demo" not in safe_json
    assert google_kit["sim"].insert_calls == 0
    assert google_kit["repo"].replay_task("task_m5").external_effects == 0


def test_denied_identity_or_revoked_token_returns_sanitized_blocked_report(google_kit):
    wrong = google_kit["principal"].model_copy(update={"scopes": ("task:read",)})
    denied = _preflight(google_kit, principal=wrong)
    assert denied.status == "BLOCKED"
    assert denied.reason_code == "READ_SCOPE_DENIED"
    assert google_kit["sim"].auth_calls == 0
    google_kit["vault"].revoke_local(
        principal=google_kit["principal"],
        connection_id=google_kit["connection"], now=NOW,
    )
    revoked = _preflight(google_kit)
    assert revoked.status == "BLOCKED"
    assert revoked.reason_code == "PROVIDER_READ_UNAVAILABLE"
    assert revoked.observation_digest is None
    assert google_kit["sim"].insert_calls == 0


@pytest.mark.parametrize("failure", ["401", "429", "oauth"])
def test_provider_rejection_fails_closed_without_leaking_error(failure, google_kit):
    google_kit["sim"].fail = failure
    result = _preflight(google_kit)
    assert result.status == "BLOCKED"
    assert result.reason_code == "PROVIDER_READ_UNAVAILABLE"
    assert result.m5_live_acceptance == "BLOCKED"
    assert "secret" not in result.model_dump_json()
    assert google_kit["sim"].insert_calls == 0


def test_unconfigured_preflight_remains_blocked_without_network(google_kit):
    result = GoogleTestAcceptancePreflight(None).run(
        principal=google_kit["principal"], task_id="task_m5",
        connection_ref=google_kit["connection"], calendar_ref="ref_calendar_m5",
        from_at=START, until_at=START + timedelta(minutes=30), now=NOW,
    )
    assert result.status == "BLOCKED"
    assert result.transport == "none"
    assert result.reason_code == "NO_TRUSTED_CONNECTION"
    assert google_kit["sim"].calls == []


def test_extra_acceptance_fields_cannot_forge_live_success(google_kit):
    report = _preflight(google_kit)
    with pytest.raises(ValidationError):
        GoogleReadOnlyPreflight.model_validate({
            **report.model_dump(), "m5_live_acceptance": "ACCEPTED",
        })
    with pytest.raises(ValidationError):
        GoogleReadOnlyPreflight.model_validate({
            **report.model_dump(), "provider_write_verified": True,
        })
    with pytest.raises(ValidationError):
        GoogleReadOnlyPreflight.model_validate({
            **report.model_dump(), "provider_calendar_id": CALENDAR,
        })
