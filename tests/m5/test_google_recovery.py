"""Offline-only Google worker: precise readback and no duplicate writes."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import update

pytest.importorskip("tal.office_execution")

from safe_agent.contracts.control import ExecutionOutcome
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.integrations.google_calendar.contracts import stable_event_id
from safe_agent.integrations.google_calendar.errors import GoogleTestWriteBlocked
from safe_agent.integrations.google_calendar.recovery import (
    GoogleDispatchDenied,
    GoogleWorkerFault,
    InjectedGoogleWorkerCrash,
    OfflineGoogleTestRecoveryWorker,
)
from safe_agent.storage.models import OutboxRow
from tests.m2.conftest import NOW
from tests.m5.conftest import CALENDAR
from tests.m5.test_google_test_outbox import stage
from tests.m5.test_google_test_outbox import staging as source_staging


@pytest.fixture(name="staging")
def _staging_alias(request: pytest.FixtureRequest):
    assert source_staging is not None
    return request.getfixturevalue("source_staging")


def worker(env, *, max_attempts=3, mapping=None):
    kit = env["kit"]
    return OfflineGoogleTestRecoveryWorker(
        kit["repo"], kit["broker"], env["service"],
        trusted_calendar_mapping=(
            {kit["calendar_ref"]: CALENDAR} if mapping is None else mapping
        ), max_attempts=max_attempts,
    )


def test_verified_insert_requires_independent_get_and_one_http_post(staging):
    intent = stage(staging)
    w = worker(staging)
    receipt = w.process_one(task_id="task_m2", now=NOW)
    assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
    assert receipt.effect_ref == stable_event_id(intent.action_hash)
    assert staging["sim"].insert_calls == 1
    repo = staging["kit"]["repo"]
    assert repo.replay_task("task_m2").phase is OfficePhase.COMPLETED
    assert repo.read_outbox("task_m2")["status"] == "VERIFIED"
    assert w.process_one(task_id="task_m2", now=NOW) is None
    assert staging["sim"].insert_calls == 1


def test_lost_insert_response_reconciles_without_reposting(staging):
    stage(staging)
    w = worker(staging)
    staging["sim"].fail = "after_insert"
    first = w.process_one(task_id="task_m2", now=NOW)
    assert first is not None and first.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["sim"].insert_calls == 1
    assert staging["kit"]["repo"].read_outbox("task_m2")["status"] == "OUTCOME_UNKNOWN"
    resumed = worker(staging).process_one(task_id="task_m2", now=NOW + timedelta(seconds=6))
    assert resumed is not None and resumed.outcome is ExecutionOutcome.COMMITTED
    assert staging["sim"].insert_calls == 1
    assert staging["kit"]["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED


def test_crash_after_insert_reconciles_expired_lease_without_duplicate(staging):
    stage(staging)
    with pytest.raises(InjectedGoogleWorkerCrash):
        worker(staging).process_one(
            task_id="task_m2", now=NOW, fault=GoogleWorkerFault.AFTER_INSERT,
        )
    assert staging["sim"].insert_calls == 1
    assert staging["kit"]["repo"].read_outbox("task_m2")["status"] == "LEASED"
    after = worker(staging).process_one(
        task_id="task_m2", now=NOW + timedelta(seconds=31),
    )
    assert after is not None and after.outcome is ExecutionOutcome.COMMITTED
    assert staging["sim"].insert_calls == 1


def test_crash_before_provider_call_never_triggers_late_first_post(staging):
    stage(staging)
    with pytest.raises(InjectedGoogleWorkerCrash):
        worker(staging).process_one(
            task_id="task_m2", now=NOW, fault=GoogleWorkerFault.AFTER_LEASE,
        )
    assert staging["sim"].insert_calls == 0
    w = worker(staging)
    uncertain = w.process_one(task_id="task_m2", now=NOW + timedelta(seconds=31))
    assert uncertain is not None and uncertain.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    review = w.process_one(task_id="task_m2", now=NOW + timedelta(seconds=37))
    assert review is not None and review.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["kit"]["repo"].replay_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert staging["sim"].insert_calls == 0


def test_expired_tal_authority_blocks_new_write(staging):
    stage(staging)
    receipt = worker(staging).process_one(
        task_id="task_m2", now=NOW + timedelta(seconds=61),
    )
    assert receipt is not None and receipt.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["kit"]["repo"].replay_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert staging["sim"].insert_calls == 0


def test_new_busy_interval_requires_review_not_a_post(staging):
    stage(staging)
    staging["sim"].busy = [
        {"start": "2026-10-15T17:00:00Z", "end": "2026-10-15T17:15:00Z"},
    ]
    result = worker(staging).process_one(task_id="task_m2", now=NOW)
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["kit"]["repo"].read_outbox("task_m2")["status"] == "NEEDS_REVIEW"
    assert staging["sim"].insert_calls == 0


def test_revoked_connection_refuses_new_effect(staging):
    stage(staging)
    staging["vault"].revoke_local(
        principal=staging["actor"], connection_id=staging["connection"], now=NOW,
    )
    result = worker(staging).process_one(task_id="task_m2", now=NOW)
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["sim"].insert_calls == 0
    assert staging["kit"]["repo"].read_task("task_m2").phase is OfficePhase.NEEDS_REVIEW


def test_wrong_private_calendar_mapping_cannot_dispatch(staging):
    stage(staging)
    other = "another-sandbox-only@group.calendar.google.com"
    result = worker(staging, mapping={
        staging["kit"]["calendar_ref"]: other,
    }).process_one(task_id="task_m2", now=NOW)
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["sim"].insert_calls == 0


def test_transient_get_error_enters_readback_only_review(staging):
    stage(staging)
    staging["sim"].fail = "429"
    w = worker(staging)
    one = w.process_one(task_id="task_m2", now=NOW)
    assert one is not None and one.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["sim"].insert_calls == 0
    staging["sim"].fail = None
    two = w.process_one(task_id="task_m2", now=NOW + timedelta(seconds=6))
    assert two is not None and two.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    three = w.process_one(task_id="task_m2", now=NOW + timedelta(seconds=12))
    assert three is not None and three.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["kit"]["repo"].read_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert staging["sim"].insert_calls == 0


def test_conflicting_deterministic_event_does_not_count_as_success(staging):
    action = stage(staging).action_hash
    eid = stable_event_id(action)
    staging["sim"].events[eid] = {
        "id": eid,
        "status": "confirmed",
        "start": {"dateTime": "2026-10-15T17:00:00Z"},
        "end": {"dateTime": "2026-10-15T17:30:00Z"},
        "extendedProperties": {"private": {
            "safe_agent_action": action,
            "safe_agent_request": "sha256:" + "0" * 64,
        }},
    }
    result = worker(staging).process_one(task_id="task_m2", now=NOW)
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert staging["kit"]["repo"].read_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert staging["sim"].insert_calls == 0


def test_provider_tag_tamper_cannot_route_to_google_worker(staging):
    action = stage(staging).action_hash
    repo = staging["kit"]["repo"]
    with repo._sessions.begin() as db:
        db.execute(update(OutboxRow).where(OutboxRow.action_hash == action).values(
            provider_kind="synthetic",
        ))
    assert worker(staging).process_one(task_id="task_m2", now=NOW) is None
    assert staging["sim"].insert_calls == 0


def test_worker_rejects_real_http_transports(staging):
    service = staging["service"]
    original = service._api._http
    direct = httpx.Client(trust_env=False)
    try:
        service._api._http = direct
        with pytest.raises(TypeError, match="OFFLINE ONLY"):
            worker(staging)
    finally:
        service._api._http = original
        direct.close()


def test_service_model_path_is_still_fully_blocked(staging):
    stage(staging)
    with pytest.raises(GoogleTestWriteBlocked):
        staging["service"].execute_model_proposal("task_m2")
    assert staging["sim"].insert_calls == 0



def test_runtime_transport_swap_refuses_claim_without_network(staging):
    stage(staging)
    w = worker(staging)
    service = staging["service"]
    original = service._api._http
    substituted = httpx.Client(trust_env=False)
    try:
        service._api._http = substituted
        with pytest.raises(GoogleDispatchDenied, match="OFFLINE_TRANSPORT_CHANGED"):
            w.process_one(task_id="task_m2", now=NOW)
    finally:
        service._api._http = original
        substituted.close()
    assert staging["sim"].insert_calls == 0
    assert staging["kit"]["repo"].read_outbox("task_m2")["attempts"] == 0
