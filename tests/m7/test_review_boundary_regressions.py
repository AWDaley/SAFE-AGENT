"""R1/R2 full synthetic application regressions; no live providers or model calls."""
from __future__ import annotations

import pytest
from sqlalchemy import update

from safe_agent.canonical import sha256
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.storage.models import FakeProviderRow, OutboxRow
from safe_agent.virtual_office.models import ObserveCalendar
from safe_agent.virtual_office.state import SyntheticOfficeState
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import plan
from tests.m6.test_synthetic_workflow import grant, new_task, queue
from tests.m7.test_coordinated_planning import build, count, input_for, reopened


def change_private_source(kit):
    """A valid synthetic edit that preserves free/busy and the calendar revision."""
    source = kit["office"]._source
    original = source.calendars[0]
    assert original.bookings
    booking = original.bookings[0].model_copy(update={"booking_id": "changed_private_booking"})
    calendar = original.model_copy(update={"bookings": (booking, *original.bookings[1:])})
    edited = source.model_copy(update={"calendars": (calendar, *source.calendars[1:])})
    edited = SyntheticOfficeState.model_validate_json(edited.model_dump_json())
    assert calendar.source_revision == original.source_revision
    assert sha256(calendar) != sha256(original)
    kit["office"].replace_synthetic_source(edited)
    with kit["repo"].engine.begin() as db:
        db.execute(update(FakeProviderRow).where(
            FakeProviderRow.organization_id == source.organization_id,
        ).values(state_json=edited.model_dump(mode="json"), state_hash=sha256(edited),
                 generation=FakeProviderRow.generation + 1))
    return calendar


def prepared(kit, monkeypatch):
    client, signer, actor, calendar, internal, service, calls, ticks = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    response = plan(client, signer, actor, task)
    assert response.status_code == 200, response.text
    saved = input_for(kit["repo"], task)
    version = grant(client, signer, kit["manager"], task, response.json())
    return client, signer, actor, calendar, internal, service, calls, ticks, task, saved, version


@pytest.mark.parametrize("fresh_process", [False, True])
def test_rejected_restore_never_publishes_changed_fingerprint(kit, monkeypatch, fresh_process):
    client, signer, actor, calendar, _, _, calls, ticks, task, saved, version = prepared(kit, monkeypatch)
    change_private_source(kit)
    active = reopened(kit) if fresh_process else kit
    try:
        if fresh_process:
            client, signer, actor, calendar, _, _, _, _ = build(
                active, monkeypatch, calls=calls, ticks=ticks,
            )
        office = active["office"]
        before = (dict(office._observations), dict(office._source_fingerprints))
        assert plan(client, signer, actor, task).status_code == 409
        assert (office._observations, office._source_fingerprints) == before
        assert queue(client, signer, actor, task, version=version).status_code == 409
        assert count(active["repo"], OutboxRow) == 0
        assert calendar.effect_count == 0 and len(calls) == 1
        if not fresh_process:
            assert office._source_fingerprints[saved.projection.revision] == saved.source_fingerprint
    finally:
        if fresh_process:
            active["repo"].close()


def test_queue_checks_durable_baseline_even_if_cache_is_corrupted(kit, monkeypatch):
    client, signer, actor, calendar, _, _, calls, _, task, saved, version = prepared(kit, monkeypatch)
    changed = change_private_source(kit)
    # Test-only corruption of the advisory cache must not replace SQL authority.
    kit["office"]._observations[saved.projection.revision] = saved.projection
    kit["office"]._source_fingerprints[saved.projection.revision] = sha256(changed)
    response = queue(client, signer, actor, task, version=version)
    assert response.status_code == 409, response.text
    assert count(kit["repo"], OutboxRow) == 0
    assert calendar.effect_count == 0 and len(calls) == 1


def test_issued_revision_fingerprint_cannot_be_rebound(kit, monkeypatch):
    _, _, _, _, _, _, _, _, task, saved, _ = prepared(kit, monkeypatch)
    change_private_source(kit)
    request = ObserveCalendar(
        organization_id=saved.context.organization_id, initiator_id=kit["principal"].actor_id,
        task_id=task, calendar_ref=saved.context.calendar_ref, customer_ref=saved.context.customer_ref,
        from_at=saved.context.allowed_start, until_at=saved.context.allowed_end,
    )
    with pytest.raises(ValueError):
        kit["office"].observe(request, as_of=saved.projection.fetched_at)
    assert kit["office"]._source_fingerprints[saved.projection.revision] == saved.source_fingerprint


def test_coordinated_worker_without_tal_cannot_create_an_effect(kit, monkeypatch):
    client, signer, actor, calendar, internal, _, calls, _, task, _, version = prepared(kit, monkeypatch)
    assert queue(client, signer, actor, task, version=version).status_code == 200
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], calendar, task_adapter=internal, office=kit["office"],
    )
    outcome = worker.process_one(task_id=task, now=NOW)
    assert outcome is not None and outcome.outcome != "COMMITTED"
    assert calendar.effect_count == internal.effect_count == 0
    assert len(calls) == 1
