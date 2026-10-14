"""Single M2 admission boundary; no connectors, effects, or completion capability."""
from __future__ import annotations

from datetime import datetime

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import ActionDecision, Approval, DecisionStatus

from .ledger import AuditLedger
from .policy import OfficeAdmission


class AdmissionService:
    def __init__(self, ledger: AuditLedger) -> None:
        self._ledger=ledger
        self._policy=OfficeAdmission()

    def propose(self, task_id: str, proposal: CalendarCreateEvent, *, now: datetime,
                expected_sequence: int, cycle: int=1,
                approval: Approval | None=None) -> ActionDecision:
        task,manifest=self._ledger.load(task_id)
        state,events=self._ledger.replay(task_id)
        if len(events)!=expected_sequence:
            raise ValueError("stale ledger sequence")
        if task.task_id!=task_id or state.name not in {
            "ASSIGNED","PLANNING","PROPOSED_UNVERIFIED","AWAITING_APPROVAL"
        }:
            raise ValueError("invalid task state")
        # Proposed state and policy admission are deliberately separate.
        entry=self._ledger.append(task_id,"PROPOSAL_RECORDED",sha256(proposal),
                                  now=now,expected_sequence=expected_sequence)
        result=self._policy.decide(task,manifest,proposal,now=now,
                                   state_version=entry.sequence,cycle=cycle,approval=approval)
        kind={DecisionStatus.DENIED:"DENIED",
              DecisionStatus.APPROVAL_REQUIRED:"APPROVAL_REQUIRED",
              DecisionStatus.AUTHORIZED:"ADMITTED"}[result.decision]
        self._ledger.append(task_id,kind,sha256(result),now=now,
                            expected_sequence=entry.sequence)
        return result
