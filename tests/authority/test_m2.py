from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from safe_agent.authority.ledger import AuditLedger
from safe_agent.authority.policy import OfficeAdmission, pin_manifest, ManifestMismatch
from safe_agent.authority.service import AdmissionService
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import Task, TaskManifest, TaskState, DecisionStatus, Approval

NOW=datetime(2026,10,13,18,tzinfo=UTC)
START=NOW+timedelta(days=1)

def values(approval=False):
    manifest=TaskManifest(manifest_id="manifest_1", manifest_version=1,
        organization_id="org_1",initiator_id="user_1",task_id="task_1",
        calendar_ref="cal_ref",customer_ref="cust_ref",permitted_operations=("calendar.create_event",),
        allowed_start=START, allowed_end=START+timedelta(hours=4),max_duration_minutes=60,
        time_zone="America/New_York",template_id="follow_up",requires_approval=approval)
    task=Task(task_id="task_1",organization_id="org_1",initiator_id="user_1",
        manifest_id="manifest_1",manifest_hash=sha256(manifest),objective_code="schedule_follow_up",
        state=TaskState.ASSIGNED,created_at=NOW,deadline=START+timedelta(days=1))
    proposal=CalendarCreateEvent(calendar_ref="cal_ref",customer_ref="cust_ref",
        start_at=START+timedelta(hours=1),end_at=START+timedelta(hours=1,minutes=30),
        time_zone="America/New_York",template_id="follow_up",simulation_ref="sim_1",
        projection_rev="projection_1")
    return task,manifest,proposal

def test_manifest_pinned():
    task,manifest,_=values()
    pin_manifest(task,manifest)
    with pytest.raises(ManifestMismatch):
        pin_manifest(task,manifest.model_copy(update={"customer_ref":"other"}))

@pytest.mark.parametrize("change,reason",[
    ({"calendar_ref":"other"},"RESOURCE_SCOPE_DENIED"),
    ({"customer_ref":"other"},"RESOURCE_SCOPE_DENIED"),
    ({"template_id":"other"},"RESOURCE_SCOPE_DENIED"),
    ({"time_zone":"UTC"},"TIME_ZONE_DENIED"),
    ({"start_at": START-timedelta(minutes=10)},"TIME_WINDOW_DENIED"),
    ({"end_at":START+timedelta(hours=3)},"DURATION_DENIED"),
])
def test_fail_closed(change,reason):
    task,manifest,proposal=values()
    proposal=proposal.model_copy(update=change)
    answer=OfficeAdmission().decide(task,manifest,proposal,now=NOW,state_version=1)
    assert answer.decision==DecisionStatus.DENIED
    assert answer.reason_code==reason

def test_admission_not_execution():
    task,manifest,proposal=values()
    answer=OfficeAdmission().decide(task,manifest,proposal,now=NOW,state_version=1)
    assert answer.decision==DecisionStatus.AUTHORIZED
    assert answer.reason_code=="ADMISSION_ONLY_NO_DISPATCH"

def test_approval_exact_hash_and_expiry():
    task,manifest,proposal=values(True)
    gate=OfficeAdmission()
    assert gate.decide(task,manifest,proposal,now=NOW,state_version=1).decision==DecisionStatus.APPROVAL_REQUIRED
    approval=Approval(approval_id="approval_1",task_id=task.task_id,approver_id="manager_1",
        proposal_hash=sha256(proposal),manifest_hash=task.manifest_hash,
        expires_at=NOW+timedelta(minutes=10),approved=True)
    assert gate.decide(task,manifest,proposal,now=NOW,state_version=1,approval=approval).decision==DecisionStatus.AUTHORIZED
    assert gate.decide(task,manifest,proposal,now=NOW+timedelta(minutes=11),state_version=1,approval=approval).decision==DecisionStatus.DENIED
    changed=proposal.model_copy(update={"simulation_ref":"sim_2"})
    assert gate.decide(task,manifest,changed,now=NOW,state_version=1,approval=approval).decision==DecisionStatus.DENIED

def test_persistent_replay_and_tamper(tmp_path):
    path=tmp_path/"m2.sqlite"
    task,manifest,proposal=values()
    db=AuditLedger(path)
    db.register(task,manifest,now=NOW)
    result=AdmissionService(db).propose(task.task_id,proposal,now=NOW,expected_sequence=1)
    assert result.decision==DecisionStatus.AUTHORIZED
    assert db.replay(task.task_id)[0]==TaskState.PROPOSED_UNVERIFIED
    assert len(db.replay(task.task_id)[1])==3
    with pytest.raises(ValueError,match="stale"):
        AdmissionService(db).propose(task.task_id,proposal,now=NOW,expected_sequence=1)
    db.close()
    reopened=AuditLedger(path)
    assert len(reopened.replay(task.task_id)[1])==3
    reopened.close()
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE events SET event_json=? WHERE task_id='task_1' AND sequence=2",('{"oops":1}',))
    with pytest.raises(Exception):
        AuditLedger(path).replay(task.task_id)

def test_duplicate_registration_rejected(tmp_path):
    task,manifest,_=values()
    db=AuditLedger(tmp_path/"m2.sqlite")
    db.register(task,manifest,now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        db.register(task,manifest,now=NOW)
