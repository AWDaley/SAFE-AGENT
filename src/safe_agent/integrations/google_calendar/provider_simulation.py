"""Provider-originated Google test-calendar simulation; never a reservation.

This trusted-only, closed contract commits a sanitized provider observation to
a single pinned task proposal. It is NOT a substitute for TAL authority, a
calendar lock, or evidence that an event was created.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import field_validator, model_validator

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import Digest, Identifier, StrictModel, require_aware
from safe_agent.contracts.control import TaskManifest

from .contracts import GoogleCalendarObservation


class GoogleProviderSimulationInvalid(ValueError):
    """The provider observation or proposed interval is not trustworthy."""


class GoogleTestProviderSimulation(StrictModel):
    schema_version: Literal["safe_agent.google_test_provider_simulation.v1"] = (
        "safe_agent.google_test_provider_simulation.v1"
    )
    provenance: Literal["google_test_calendar"] = "google_test_calendar"
    organization_id: Identifier
    task_id: Identifier
    calendar_ref: Identifier
    connection_ref: Identifier
    observation_revision: Digest
    observation_hash: Digest
    proposal_hash: Digest
    start_at: datetime
    end_at: datetime
    observed_at: datetime
    expires_at: datetime
    advisory_only: Literal[True] = True
    holds_slot: Literal[False] = False
    proposed_slot_available: Literal[True] = True
    simulation_hash: Digest

    @field_validator("start_at", "end_at", "observed_at", "expires_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def valid_window(self) -> GoogleTestProviderSimulation:
        if (self.start_at >= self.end_at or self.observed_at >= self.expires_at
                or self.holds_slot is not False
                or self.advisory_only is not True):
            raise ValueError("invalid advisory Google simulation interval")
        return self

    def verify_hash(self) -> None:
        if sha256(self.model_dump(mode="python", exclude={"simulation_hash"})) != self.simulation_hash:
            raise GoogleProviderSimulationInvalid("GOOGLE_PROVIDER_SIMULATION_HASH_MISMATCH")


def simulate_provider_proposal(
    *, observation: GoogleCalendarObservation,
    proposal: CalendarCreateEvent,
    manifest: TaskManifest,
    now: datetime,
) -> GoogleTestProviderSimulation:
    """Deterministically match one immutable proposal to trusted free/busy.

    A different provider observation or a later time cannot be silently
    substituted. Google free/busy is advisory: it never reserves the slot.
    """
    now = require_aware(now)
    if type(observation) is not GoogleCalendarObservation:
        raise GoogleProviderSimulationInvalid("GOOGLE_OBSERVATION_TYPE_INVALID")
    if type(proposal) is not CalendarCreateEvent or type(manifest) is not TaskManifest:
        raise GoogleProviderSimulationInvalid("GOOGLE_PROPOSAL_OR_MANIFEST_INVALID")
    expected_revision = sha256({
        "organization_id": observation.organization_id,
        "task_id": observation.task_id, "calendar_ref": observation.calendar_ref,
        "connection_ref": observation.connection_ref, "free_slots": observation.free_slots,
        "from_at": observation.from_at, "until_at": observation.until_at,
        "fetched_at": observation.fetched_at, "expires_at": observation.expires_at,
    })
    if (observation.provenance != "google_test_calendar"
            or observation.advisory_only is not True
            or expected_revision != observation.revision
            or manifest.organization_id != observation.organization_id
            or manifest.task_id != observation.task_id
            or manifest.calendar_ref != observation.calendar_ref
            or manifest.customer_ref != proposal.customer_ref
            or manifest.calendar_ref != proposal.calendar_ref
            or manifest.template_id != proposal.template_id
            or manifest.time_zone != proposal.time_zone
            or proposal.projection_rev == ""
            or proposal.start_at < manifest.allowed_start
            or proposal.end_at > manifest.allowed_end
            or proposal.end_at <= proposal.start_at
            or (proposal.end_at - proposal.start_at).total_seconds() >
                manifest.max_duration_minutes * 60
            or not observation.from_at <= proposal.start_at
            or not proposal.end_at <= observation.until_at
            or now < observation.fetched_at or now >= observation.expires_at
            or not any(slot.start_at <= proposal.start_at and
                       proposal.end_at <= slot.end_at
                       for slot in observation.free_slots)):
        raise GoogleProviderSimulationInvalid("GOOGLE_PROVIDER_SIMULATION_UNAVAILABLE")
    content: dict[str, object] = {
        "schema_version": "safe_agent.google_test_provider_simulation.v1",
        "provenance": "google_test_calendar",
        "organization_id": observation.organization_id,
        "task_id": observation.task_id,
        "calendar_ref": observation.calendar_ref,
        "connection_ref": observation.connection_ref,
        "observation_revision": observation.revision,
        "observation_hash": sha256(observation),
        "proposal_hash": sha256(proposal),
        "start_at": proposal.start_at,
        "end_at": proposal.end_at,
        "observed_at": observation.fetched_at,
        "expires_at": observation.expires_at,
        "advisory_only": True,
        "holds_slot": False,
        "proposed_slot_available": True,
    }
    return GoogleTestProviderSimulation.model_validate({
        **content, "simulation_hash": sha256(content),
    })
