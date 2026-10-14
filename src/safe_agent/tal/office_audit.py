"""Read-only SAFE ledger -> TAL shadow-journal reconciliation (synthetic only).

TAL's SQLite office journal is deliberately a *separate transaction* from the
SAFE SQL outbox. The SAFE ledger is authoritative for execution and read-back.
This trusted mirror can catch up after an interruption, but cannot grant a new
action or infer provider success without SAFE's independently verified receipt.
"""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from typing import Any

from safe_agent.contracts.control import Task
from safe_agent.evidence.ledger import EventKind, OfficePhase
from safe_agent.storage.repository import LedgerRepository


class TALOfficeAuditSyncError(ValueError):
    """Audit is missing, inconsistent, or unavailable; never an execution permit."""


class TrustedTALOfficeAuditMirror:
    """Synchronize *already committed* SAFE events to a separately durable TAL journal.

    Caller must be a trusted backend worker or operator; never expose this
    method or its journal to an agent, model, or unauthenticated web route.
    """

    def __init__(self, journal: object) -> None:
        if journal is None:
            raise ValueError("TAL office journal must be configured")
        self._journal = journal

    def reconcile(self, repo: LedgerRepository, task_id: str, *, now: datetime) -> str:
        """Reconcile without writing to the synthetic provider or granting authority.

        Requires full SAFE hash-chain replay and outbox/receipt cross-check. A
        missing admission is *not* backfilled: after-the-fact admission would be
        an unsafe new authorization, especially after expiry.
        """
        if now.utcoffset() is None:
            raise TALOfficeAuditSyncError("AUDIT_TIME_MUST_BE_AWARE")
        try:
            upstream = import_module("tal.office_journal")
            # Only an independently tested TAL audit implementation is used.
            if not isinstance(self._journal, upstream.SQLiteOfficeJournal):
                raise TALOfficeAuditSyncError("TAL_JOURNAL_TYPE_NOT_PINNED")
            snap = repo.replay_task(task_id)
            task = repo.read_task_record(task_id)
            if not isinstance(task, Task):
                raise TALOfficeAuditSyncError("TAL_AUDIT_CALENDAR_ONLY")
            outbox = repo.read_outbox(task_id)
            action_hash = outbox["action_hash"]
            if not isinstance(action_hash, str):
                raise TALOfficeAuditSyncError("TAL_ACTION_ID_INVALID")
            tal_state = self._journal.replay(action_hash)
            if (
                tal_state.task_id != task_id
                or tal_state.action_hash != action_hash
                or tal_state.source_manifest_hash != task.manifest_hash
                or tal_state.source_proposal_hash != snap.proposal_hash
            ):
                raise TALOfficeAuditSyncError("TAL_AUDIT_BINDING_MISMATCH")
            if self._journal.events(action_hash)[0].reason_code != "TRUSTED_ADMISSION_ONLY":
                raise TALOfficeAuditSyncError("TAL_AUDIT_ADMISSION_DENIED")
            events = repo.read_events(task_id)
            status = outbox["status"]
            kind: Any
            evidence: Any = None
            reason: str
            if snap.phase is OfficePhase.COMPLETED and status == "VERIFIED":
                final = events[-1]
                if (
                    final.kind is not EventKind.ACTION_VERIFIED
                    or final.payload.get("verification_hash") != snap.verification_hash
                    or snap.verification_hash is None
                ):
                    raise TALOfficeAuditSyncError("TAL_SAFE_VERIFICATION_MISSING")
                evidence = upstream.OfficeVerifiedEvidence(
                    action_hash=action_hash,
                    task_id=task_id,
                    source_manifest_hash=task.manifest_hash,
                    source_proposal_hash=snap.proposal_hash,
                    observation_hash=snap.verification_hash,
                    observed_at=final.occurred_at,
                )
                kind = upstream.OfficeJournalKind.VERIFIED
                reason = "SYNTHETIC_READBACK_MATCHED"
            elif snap.phase is OfficePhase.NEEDS_REVIEW and status == "NEEDS_REVIEW":
                kind = upstream.OfficeJournalKind.NEEDS_REVIEW
                reason = "SAFE_ACTION_NEEDS_REVIEW"
            elif snap.phase is OfficePhase.EXECUTING and status == "OUTCOME_UNKNOWN":
                kind = upstream.OfficeJournalKind.OUTCOME_UNKNOWN
                reason = "SAFE_OUTCOME_UNRESOLVED"
            elif snap.phase is OfficePhase.EXECUTING and status in {"QUEUED", "LEASED"}:
                # No new evidence since TAL admission. A lease alone proves no outcome.
                return str(tal_state.state.value)
            else:
                raise TALOfficeAuditSyncError("TAL_SAFE_OUTBOX_STATE_CONFLICT")
            # Do not flood TAL with duplicate uncertainty events. Completed
            # receipts are checked for exact evidence by upstream TAL itself.
            if kind in {upstream.OfficeJournalKind.NEEDS_REVIEW,
                        upstream.OfficeJournalKind.OUTCOME_UNKNOWN} and tal_state.state == kind:
                return str(kind.value)
            # Idempotent verified callbacks are handled by upstream TAL; an
            # incompatible terminal transition is rejected instead of rewritten.
            result = self._journal.record_outcome(
                action_hash=action_hash, kind=kind, evidence=evidence,
                reason_code=reason, now=now,
            )
            if result.state != kind:
                raise TALOfficeAuditSyncError("TAL_AUDIT_TRANSITION_MISMATCH")
            return str(result.state.value)
        except TALOfficeAuditSyncError:
            raise
        except Exception as error:
            # Do not accidentally include raw SQL/provider error text in status.
            raise TALOfficeAuditSyncError("TAL_AUDIT_RECONCILIATION_REQUIRED") from error
