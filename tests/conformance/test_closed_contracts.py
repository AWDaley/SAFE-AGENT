"""M0 negative conformance tests for model-visible proposals."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from safe_agent.contracts.actions import CalendarCreateEvent, parse_proposal_json
from safe_agent.contracts.control import TaskBudget, TaskManifest


@pytest.fixture
def proposal() -> dict[str, str]:
    return {
        "schema_version": "safe_agent.proposal.v1",
        "operation": "calendar.create_event",
        "calendar_ref": "calendar_scope_01",
        "customer_ref": "customer_ref_a9",
        "start_at": "2026-10-15T14:00:00-04:00",
        "end_at": "2026-10-15T14:30:00-04:00",
        "time_zone": "America/New_York",
        "template_id": "follow_up_v1",
        "simulation_ref": "simulation_demo_01",
        "projection_rev": "projection_42",
    }


def test_valid_single_proposal(proposal: dict[str, str]) -> None:
    result = parse_proposal_json(json.dumps(proposal))
    assert isinstance(result, CalendarCreateEvent)
    assert result.operation == "calendar.create_event"


@pytest.mark.parametrize(
    "injection",
    [
        {"operation": "execute_command"},
        {"operation": "calendar.delete_event"},
        {"schema_version": "safe_agent.proposal.v999"},
        {"shell_command": "rm -rf /"},
        {"role": "admin"},
        {"manifest_hash": "sha256:" + "0" * 64},
        {"provider_credentials": "DO_NOT_EXPOSE"},
        {"end_at": "2026-10-15T13:00:00-04:00"},
        {"start_at": "2026-10-15T14:00:00"},
    ],
)
def test_malformed_or_privileged_proposal_denied(
    proposal: dict[str, str], injection: dict[str, str]
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_proposal_json(json.dumps({**proposal, **injection}))


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "null",
        "{}",
        '"calendar.create_event"',
        '{"operation":"calendar.create_event"}{"operation":"calendar.create_event"}',
        '{"operation":"calendar.create_event", "operation":"calendar.create_event"}',
        '{"calendar_ref":"scope_a","calendar_ref":"scope_b"}',
        '[{"operation":"calendar.create_event"},{"operation":"calendar.create_event"}]',
        '{"operation":NaN}',
        " " * 16_385,
    ],
)
def test_no_multiproposal_or_duplicate_json(payload: str) -> None:
    with pytest.raises((ValidationError, ValueError, json.JSONDecodeError)):
        parse_proposal_json(payload)


def test_schema_is_frozen(proposal: dict[str, str]) -> None:
    action = parse_proposal_json(json.dumps(proposal))
    assert isinstance(action, CalendarCreateEvent)
    with pytest.raises(ValidationError):
        action.calendar_ref = "another_calendar"


def test_manifest_deny_on_unknown_operation(demo: dict[str, object]) -> None:
    manifest = demo["manifest"]
    assert isinstance(manifest, dict)
    with pytest.raises(ValidationError):
        TaskManifest.model_validate_json(
            json.dumps({**manifest, "permitted_operations": ["calendar.delete_event"]})
        )


def test_manifest_deny_on_unrecognized_timezone(demo: dict[str, object]) -> None:
    manifest = demo["manifest"]
    assert isinstance(manifest, dict)
    with pytest.raises(ValidationError):
        TaskManifest.model_validate_json(json.dumps({**manifest, "time_zone": "Mars/Phobos"}))


def test_manifest_deny_on_naive_window(demo: dict[str, object]) -> None:
    manifest = demo["manifest"]
    assert isinstance(manifest, dict)
    with pytest.raises(ValidationError):
        TaskManifest.model_validate_json(
            json.dumps({**manifest, "allowed_start": "2026-10-15T13:00:00"})
        )


def test_budget_cannot_allow_two_effects_per_cycle() -> None:
    with pytest.raises(ValidationError):
        TaskBudget(max_effects_per_cycle=2)  # type: ignore[arg-type]


def test_strict_python_objects_reject_coercion(proposal: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        CalendarCreateEvent.model_validate(proposal)
    assert datetime.fromisoformat(proposal["start_at"]).utcoffset() is not None
