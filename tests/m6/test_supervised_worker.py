"""M6-01d: supervised, bounded synthetic-only outbox worker acceptance.

These tests never construct a live provider adapter or permit a browser to
call the worker. Worker leases/receipts remain authoritative in the SQL ledger.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import Event

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from safe_agent.demo.m6_worker import run_m6_worker_demo
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import FaultPoint, InjectedCrash, SyntheticRecoveryWorker
from safe_agent.execution.supervisor import (
    SupervisedSyntheticWorker,
    SyntheticSupervisorPolicy,
)
from safe_agent.storage.models import OutboxRow
from tests.m2.conftest import NOW
from tests.m6.test_synthetic_workflow import grant, new_task, prepare, queue, rig


def supervised(kit, calendar, internal, *, now=NOW, policy=None):
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], calendar,
        task_adapter=internal, office=kit["office"],
    )
    service = SupervisedSyntheticWorker(worker, clock=lambda: now, policy=policy)
    return worker, service


def queue_calendar(kit, client, signer, employee):
    task_id = new_task(client, signer, employee)
    proposed = prepare(client, signer, employee, task_id)
    assert proposed.status_code == 200, proposed.text
    version = grant(client, signer, kit["manager"], task_id, proposed.json())
    staged = queue(client, signer, employee, task_id, version=version)
    assert staged.status_code == 200, staged.text
    return task_id


def queue_internal(client, signer, employee, *, key="b"):
    task_id = new_task(
        client, signer, employee, option="opt_backoffice",
        params={"idempotency_key": "req_" + key * 32},
    )
    proposed = prepare(client, signer, employee, task_id)
    assert proposed.status_code == 200, proposed.text
    staged = queue(
        client, signer, employee, task_id,
        version=proposed.json()["state_version"],
    )
    assert staged.status_code == 200, staged.text
    return task_id


def test_calendar_is_verified_by_supervisor_not_by_http(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    task_id = queue_calendar(kit, client, signer, employee)
    assert kit["repo"].read_task(task_id).phase is OfficePhase.EXECUTING
    assert calendar.effect_count == 0

    _, service = supervised(kit, calendar, internal)
    result = service.poll_once()
    assert result.status == "PROCESSED"
    assert result.receipts == 1
    assert result.verified == 1 and result.unverified == 0
    assert result.infrastructure_errors == 0
    assert calendar.effect_count == 1
    finished = kit["repo"].replay_task(task_id)
    assert finished.phase is OfficePhase.COMPLETED
    assert finished.external_effects == 1 and finished.verification_hash is not None
    assert kit["repo"].read_outbox(task_id)["status"] == "VERIFIED"

    idle = service.poll_once()
    assert idle.status == "IDLE"
    assert idle.receipts == 0
    assert calendar.effect_count == 1
    for sensitive in ("cal_main_private", "cust_c1", "employee_demo", "ref_"):
        assert sensitive not in result.model_dump_json()


def test_internal_office_action_completes_with_separate_fake_verifier(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    task_id = queue_internal(client, signer, employee)
    _, service = supervised(kit, calendar, internal)
    stop = Event()
    result = service.serve(stop, max_passes=1)
    assert result.stop_reason == "PASS_LIMIT"
    assert result.passes == 1 and result.verified == result.receipts == 1
    assert result.synthetic_only is True
    assert result.live_provider_writes_enabled is False
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.COMPLETED
    assert internal.effect_count == 1 and calendar.effect_count == 0


def test_one_supervised_pass_enforces_claim_budget_for_two_queued_tasks(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    calendar_task = queue_calendar(kit, client, signer, employee)
    internal_task = queue_internal(client, signer, employee)
    policy = SyntheticSupervisorPolicy(
        max_claims_per_pass=1, idle_poll_seconds=0.01,
    )
    _, service = supervised(kit, calendar, internal, policy=policy)
    one = service.poll_once()
    assert one.verified == one.receipts == 1
    assert one.exhausted_batch is True
    assert kit["repo"].replay_task(calendar_task).phase in {
        OfficePhase.COMPLETED, OfficePhase.EXECUTING,
    }
    assert kit["repo"].replay_task(internal_task).phase in {
        OfficePhase.COMPLETED, OfficePhase.EXECUTING,
    }
    assert calendar.effect_count + internal.effect_count == 1
    two = service.poll_once()
    assert two.verified == 1 and two.receipts == 1
    assert calendar.effect_count == internal.effect_count == 1
    assert kit["repo"].replay_task(calendar_task).phase is OfficePhase.COMPLETED
    assert kit["repo"].replay_task(internal_task).phase is OfficePhase.COMPLETED


def test_supervisor_recovers_lost_response_without_duplicate_effect(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    task_id = queue_calendar(kit, client, signer, employee)
    original_worker, _ = supervised(kit, calendar, internal)
    ambiguous = original_worker.process_one(
        task_id=task_id, now=NOW, fault=FaultPoint.RESPONSE_LOST,
    )
    assert ambiguous is not None and ambiguous.outcome == "OUTCOME_UNKNOWN"
    assert calendar.effect_count == 1
    assert kit["repo"].read_outbox(task_id)["status"] == "OUTCOME_UNKNOWN"

    _, too_soon = supervised(kit, calendar, internal, now=NOW + timedelta(seconds=3))
    assert too_soon.poll_once().status == "IDLE"
    _, after_retry_due = supervised(kit, calendar, internal, now=NOW + timedelta(seconds=6))
    recovered = after_retry_due.poll_once()
    assert recovered.verified == 1 and recovered.unverified == 0
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.COMPLETED
    assert calendar.effect_count == 1


def test_lease_blocks_resume_until_expiry_after_postcommit_crash(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    task_id = queue_calendar(kit, client, signer, employee)
    crashed, _ = supervised(kit, calendar, internal)
    with pytest.raises(InjectedCrash):
        crashed.process_one(
            task_id=task_id, now=NOW, fault=FaultPoint.AFTER_PROVIDER_COMMIT,
        )
    assert calendar.effect_count == 1
    _, early = supervised(kit, calendar, internal, now=NOW + timedelta(seconds=10))
    assert early.poll_once().status == "IDLE"
    _, recovered = supervised(kit, calendar, internal, now=NOW + timedelta(seconds=31))
    assert recovered.poll_once().verified == 1
    assert kit["repo"].replay_task(task_id).phase is OfficePhase.COMPLETED
    assert calendar.effect_count == 1


def test_google_bound_outbox_cannot_be_claimed_by_supervisor(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    task_id = queue_internal(client, signer, employee)
    # This fixture deliberately simulates forbidden cross-provider tagging.
    # The worker must never claim a non-synthetic outbox row.
    with kit["repo"]._sessions.begin() as session:
        session.execute(
            update(OutboxRow).where(OutboxRow.task_id == task_id)
            .values(provider_kind="google_test")
        )
    _, service = supervised(kit, calendar, internal)
    pass_result = service.poll_once()
    assert pass_result.status == "IDLE" and pass_result.receipts == 0
    assert kit["repo"].read_outbox(task_id)["status"] == "QUEUED"
    assert internal.effect_count == calendar.effect_count == 0
    # Reclassifying a synthetic row as Google without the sealed TAL/provider
    # binding must also fail independent ledger replay, not appear valid.
    with pytest.raises(ValueError, match="Google outbox authorization/binding"):
        kit["repo"].replay_task(task_id)


def test_stop_signal_prevents_processing_any_queued_action(kit):
    client, signer, employee, calendar, internal, _ = rig(kit)
    task_id = queue_internal(client, signer, employee)
    stop = Event()
    stop.set()
    _, service = supervised(kit, calendar, internal)
    result = service.serve(stop)
    assert result.stop_reason == "STOP_REQUESTED"
    assert result.passes == 0 and result.receipts == 0
    assert internal.effect_count == calendar.effect_count == 0
    assert kit["repo"].read_outbox(task_id)["status"] == "QUEUED"


def test_idle_poll_wait_is_bounded_and_interruptible(kit):
    _, _, _, calendar, internal, _ = rig(kit)
    _, service = supervised(kit, calendar, internal, policy=SyntheticSupervisorPolicy(
        max_claims_per_pass=1, idle_poll_seconds=0.25,
    ))

    class StopOnWait(Event):
        def __init__(self):
            super().__init__()
            self.waited: list[float] = []

        def wait(self, timeout=None):
            self.waited.append(timeout)
            self.set()
            return True

    stop = StopOnWait()
    result = service.serve(stop)
    assert result.stop_reason == "STOP_REQUESTED"
    assert result.passes == 1 and result.idle_passes == 1
    assert stop.waited == [0.25]


def test_unexpected_worker_errors_back_off_then_fail_closed(kit, monkeypatch):
    _, _, _, calendar, internal, _ = rig(kit)
    worker, service = supervised(
        kit, calendar, internal,
        policy=SyntheticSupervisorPolicy(
            max_consecutive_errors=3, error_backoff_seconds=0.2,
        ),
    )

    def broken_process(*, now):
        raise RuntimeError("PRIVATE_SECRET_SHOULD_NOT_LEAK")

    monkeypatch.setattr(worker, "process_one", broken_process)

    class RecordWait(Event):
        def __init__(self):
            super().__init__()
            self.waited: list[float] = []

        def wait(self, timeout=None):
            self.waited.append(timeout)
            return False

    stop = RecordWait()
    report = service.serve(stop, max_passes=9)
    assert report.stop_reason == "WORKER_UNHEALTHY"
    assert report.passes == 3
    assert report.infrastructure_errors == 3
    assert report.verified == report.receipts == 0
    assert stop.waited == pytest.approx([0.2, 0.4])
    assert service.halted is True
    assert service.poll_once().status == "HALTED"
    assert "PRIVATE_SECRET_SHOULD_NOT_LEAK" not in report.model_dump_json()


def test_worker_policy_and_clock_are_fail_closed(kit):
    _, _, _, calendar, internal, _ = rig(kit)
    worker, _ = supervised(kit, calendar, internal)
    with pytest.raises(TypeError):
        SupervisedSyntheticWorker(object())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SyntheticSupervisorPolicy(max_claims_per_pass=0)
    with pytest.raises(ValidationError):
        SyntheticSupervisorPolicy(idle_poll_seconds=0.0)
    with pytest.raises(ValidationError):
        SyntheticSupervisorPolicy(max_consecutive_errors=0)
    _, invalid = supervised(
        kit, calendar, internal, now=datetime(2026, 10, 14, 13, 0),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        invalid.poll_once()
    with pytest.raises(ValueError):
        invalid.serve(Event(), max_passes=0)
    assert worker is not None


def test_supervisor_demo_and_no_new_http_worker_route(kit):
    output = run_m6_worker_demo()
    assert output["mode"] == "M6_SUPERVISED_SYNTHETIC_ONLY"
    assert output["task_phase"] == "COMPLETED"
    assert output["verified_effects"] == 1
    assert output["fake_provider_effects"] == 1
    assert output["real_provider_writes_enabled"] is False
    assert output["m5_live_acceptance"] == "BLOCKED"
    for private in ("cal_main_private", "cust_c1", "ref_", "refresh_token"):
        assert private not in str(output)
    client, _, _, _, _, _ = rig(kit)
    assert client.post("/v1/worker/run").status_code == 404
    assert client.get("/v1/worker/status").status_code == 404
