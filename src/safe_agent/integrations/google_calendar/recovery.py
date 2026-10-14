"""PR3 privileged Google test execution and reconciliation, OFFLINE MOCKS ONLY.

Only a trusted operator test harness may instantiate this worker. It accepts
exact httpx.MockTransport clients, never a real network transport, and is not
registered with any agent, FastAPI route, or general-purpose dispatcher.

New writes require the bound SQL outbox, current TAL authorization, private
calendar mapping, broker scope, fresh free/busy and a first-ever claim.
Once a write could have happened, subsequent claims do READBACK ONLY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from importlib import import_module
from secrets import token_hex
from typing import Any, cast

import httpx

from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.control import (
    ExecutionOutcome,
    ExecutionReceipt,
    Task,
    TaskManifest,
)
from safe_agent.evidence.ledger import EventKind, OfficePhase, TaskSnapshot
from safe_agent.security.principal import AccessDenied, Principal
from safe_agent.storage.repository import LedgerRepository, OutboxClaim, StateConflict

from .client import _validate_calendar_id
from .contracts import GoogleTestEventIntent, stable_event_id
from .errors import (
    GoogleCalendarError,
    GoogleConnectionDenied,
    GoogleRateLimited,
    GoogleStateConflict,
    GoogleUnknownOutcome,
)
from .outbox import _trusted_tal_office_command
from .service import GoogleTestCalendarService


class GoogleWorkerFault(StrEnum):
    AFTER_LEASE = "after_lease"
    AFTER_INSERT = "after_insert"


class InjectedGoogleWorkerCrash(BaseException):
    """Test-only simulated process exit: leased intent survives."""


class GoogleDispatchDenied(ValueError):
    """A new Google action is not permitted; never use this as a retry signal."""


@dataclass(frozen=True)
class _Dispatch:
    task: Task
    manifest: TaskManifest
    proposal: CalendarCreateEvent
    snapshot: TaskSnapshot
    authority: Any
    tal_action: object
    binding: dict[str, str]
    intent: GoogleTestEventIntent


class OfflineGoogleTestRecoveryWorker:
    """One narrowly scoped TEST WORKER; no external Google connectivity allowed.

    The authorized calendar mapping is trusted deployment/test configuration,
    NEVER model input. It maps task-scoped opaque handles to exact allowlisted
    provider calendar IDs. Both HTTP clients must use MockTransport: the worker
    cannot issue live OAuth or Calendar requests, including through refresh.
    """

    def __init__(
        self, repo: LedgerRepository, broker: ReferenceBroker,
        service: GoogleTestCalendarService,
        *, trusted_calendar_mapping: dict[str, str],
        worker_id: str | None = None, max_attempts: int = 3,
    ) -> None:
        if type(service) is not GoogleTestCalendarService:
            raise TypeError("only the concrete test Google service is supported")
        if (type(service._api._http._transport) is not httpx.MockTransport
                or type(service._oauth._http._transport) is not httpx.MockTransport):
            raise TypeError("PR3 worker is OFFLINE ONLY: HTTP MockTransport is mandatory")
        if not 1 <= max_attempts <= 10:
            raise ValueError("invalid reconciliation limit")
        if not trusted_calendar_mapping:
            raise ValueError("explicit trusted test calendar mapping required")
        self._calendar_mapping = {
            opaque: _validate_calendar_id(cid)
            for opaque, cid in trusted_calendar_mapping.items()
        }
        if any(not opaque or not opaque.startswith("ref_") for opaque in self._calendar_mapping):
            raise ValueError("only broker-minted opaque calendar references may be mapped")
        self._repo = repo
        self._broker = broker
        self._service = service
        self._id = worker_id or "google_test_worker_" + token_hex(8)
        self._max_attempts = max_attempts

    def _assert_offline_clients(self) -> None:
        # Check at use time as well as constructor time. A backend swapping the
        # client after construction must not accidentally create live egress.
        if (type(self._service._api._http._transport) is not httpx.MockTransport
                or type(self._service._oauth._http._transport) is not httpx.MockTransport):
            raise GoogleDispatchDenied("GOOGLE_OFFLINE_TRANSPORT_CHANGED")

    def process_one(
        self, *, now: datetime, task_id: str | None = None,
        fault: GoogleWorkerFault | None = None,
    ) -> ExecutionReceipt | None:
        """Claim and process at most one intent; never blindly reissue an insert."""
        now = require_aware(now)
        self._assert_offline_clients()
        claim = self._repo.claim_google_test_action(
            worker_id=self._id, now=now, task_id=task_id,
        )
        if claim is None:
            return None
        if fault is GoogleWorkerFault.AFTER_LEASE:
            raise InjectedGoogleWorkerCrash("injected exit after durable lease")
        try:
            return self._process_claim(claim, now=now, fault=fault)
        except InjectedGoogleWorkerCrash:
            raise
        except (GoogleUnknownOutcome, GoogleRateLimited):
            return self._unknown(claim, now=now, reason="GOOGLE_READBACK_UNAVAILABLE")
        except (GoogleStateConflict, GoogleDispatchDenied, GoogleConnectionDenied,
                GoogleCalendarError, AccessDenied, StateConflict, ValueError):
            return self._review(claim, now=now, reason="GOOGLE_SCOPE_OR_CONFLICT")
        except Exception:
            # Any unexpected fault may have occurred after provider commit.
            # Reconcile first; an exception is NEVER evidence of no effect.
            return self._unknown(claim, now=now, reason="GOOGLE_EFFECT_OUTCOME_UNKNOWN")

    def _context(self, claim: OutboxClaim) -> _Dispatch:
        # Full SAFE hash-chain replay before touching even a mock provider.
        self._repo.replay_task(claim.task_id)
        binding = self._repo.read_google_test_action_binding(
            claim.task_id, claim.action_hash,
        )
        authority = cast(Any, self._repo.read_office_execution_authorization(
            claim.task_id, claim.action_hash,
        ))
        with self._repo.write_task(claim.task_id) as tx:
            task, manifest, snap = tx.task, tx.manifest, tx.snapshot
            row = tx.read_outbox(claim.action_hash)
            proposal = tx.read_active_proposal()
            if (claim.provider_kind != "google_test"
                    or not isinstance(task, Task)
                    or not isinstance(manifest, TaskManifest)
                    or not isinstance(proposal, CalendarCreateEvent)
                    or snap.phase is not OfficePhase.EXECUTING
                    or snap.proposal_hash != claim.proposal_hash
                    or sha256(proposal) != claim.proposal_hash
                    or task.manifest_hash != claim.manifest_hash
                    or sha256(manifest) != claim.manifest_hash
                    or row.provider_kind != "google_test"
                    or row.status != "LEASED"
                    or row.lease_owner != claim.worker_id
                    or row.attempts != claim.attempts
                    or row.operation != "calendar.create_event"
                    or row.proposal_hash != claim.proposal_hash
                    or row.source_revision != claim.source_revision
                    or row.source_hash != claim.source_hash
                    or binding["action_hash"] != claim.action_hash
                    or binding["calendar_ref"] != manifest.calendar_ref
                    or binding["connection_ref"] == ""
                    or binding["authorization_hash"] != authority.authorization_hash
                    or authority.state_version != snap.state_version):
                raise GoogleDispatchDenied("GOOGLE_TRUSTED_INTENT_MISMATCH")
            tal_action = _trusted_tal_office_command(
                tx, action_hash=claim.action_hash,
            )
            intent = GoogleTestEventIntent(
                action_hash=claim.action_hash,
                request_hash=binding["provider_request_hash"],
                start_at=proposal.start_at, end_at=proposal.end_at,
                time_zone=proposal.time_zone,
            )
        return _Dispatch(task=task, manifest=manifest, proposal=proposal,
                         snapshot=snap, authority=authority,
                         tal_action=tal_action, binding=binding, intent=intent)

    def _connection(self, context: _Dispatch, *, now: datetime) -> tuple[str, str]:
        # This principal comes only from the registered, replay-verified SQL task.
        # It is a confined, short-lived worker delegation for OAuth refresh, not
        # an externally mintable user token or agent-controlled identity.
        principal = Principal(
            organization_id=context.task.organization_id,
            actor_id=context.task.initiator_id,
            scopes=("connection:read",),
            authenticated_until=now + timedelta(minutes=1),
        )
        self._assert_offline_clients()
        token, actual_calendar = self._service._oauth.access_token(
            principal=principal, connection_id=context.binding["connection_ref"],
            now=now,
        )
        exact = self._calendar_mapping.get(context.manifest.calendar_ref)
        if exact is None or actual_calendar != exact:
            raise GoogleDispatchDenied("GOOGLE_PRIVATE_CALENDAR_MAPPING_MISMATCH")
        return token, actual_calendar

    def _require_new_effect(self, claim: OutboxClaim, context: _Dispatch,
                            *, now: datetime) -> None:
        # TAL's record is necessary but insufficient: recheck task and broker
        # scopes, budget, approval, state and lease before any first POST.
        if claim.attempts != 1 or claim.preceding_status != "QUEUED":
            raise GoogleDispatchDenied("GOOGLE_RETRY_IS_READBACK_ONLY")
        try:
            rt = import_module("tal.runtime")
            store = import_module("tal.storage")
            adapters = import_module("tal.adapters")
            engine = rt.TALRuntime(
                store.InMemoryEventStore(), adapters.FakeToolRegistry(),
                clock=lambda: now,
            )
            engine.require_office_execution_authorization(
                context.authority, context.tal_action,
                actor_id=context.task.initiator_id,
                state_version=context.snapshot.state_version,
            )
        except Exception as error:
            raise GoogleDispatchDenied("GOOGLE_TAL_AUTHORITY_INVALID") from error
        if (now >= context.task.deadline
                or context.snapshot.external_effects >=
                    context.manifest.budget.max_external_effects
                or context.snapshot.cycles_used > context.manifest.budget.max_cycles):
            raise GoogleDispatchDenied("GOOGLE_TASK_DEADLINE_OR_BUDGET_EXPIRED")
        self._broker.trusted_import(self._repo.load_protected_references(
            claim.task_id, claim.organization_id,
        ))
        scope = BrokerScope(
            organization_id=claim.organization_id,
            initiator_id=context.task.initiator_id,
            task_id=claim.task_id, operation="calendar.create_event", now=now,
        )
        self._broker.resolve(context.manifest.calendar_ref, scope=scope,
                             kind=ResourceKind.CALENDAR)
        self._broker.resolve(context.manifest.customer_ref, scope=scope,
                             kind=ResourceKind.CUSTOMER)
        with self._repo.write_task(claim.task_id) as tx:
            row = tx.read_outbox(claim.action_hash)
            if (tx.snapshot != context.snapshot or row.status != "LEASED"
                    or row.provider_kind != "google_test"
                    or row.lease_owner != claim.worker_id
                    or row.attempts != claim.attempts):
                raise GoogleDispatchDenied("GOOGLE_LEASE_OR_STATE_CHANGED")

    def _process_claim(
        self, claim: OutboxClaim, *, now: datetime,
        fault: GoogleWorkerFault | None,
    ) -> ExecutionReceipt:
        context = self._context(claim)
        token, calendar_id = self._connection(context, now=now)
        # Always first query by the deterministic id. This is authoritative
        # source readback, not a free/busy inference of successful insertion.
        self._assert_offline_clients()
        found = self._service._api.find_effect(
            calendar_id=calendar_id, token=token, intent=context.intent,
        )
        if found is not None:
            return self._verified(claim, context, found, now=now)
        if claim.attempts > 1 or claim.preceding_status != "QUEUED":
            # A prior worker might have POSTed before losing its response.
            # Even a 404 on this pass is NOT permission to retry the POST.
            return self._unknown(claim, now=now, reason="GOOGLE_NOT_YET_OBSERVED")
        self._require_new_effect(claim, context, now=now)
        self._assert_offline_clients()
        intervals = self._service._api.busy_intervals(
            calendar_id=calendar_id, token=token,
            start=context.intent.start_at, end=context.intent.end_at,
        )
        if intervals:
            raise GoogleDispatchDenied("GOOGLE_SLOT_NO_LONGER_AVAILABLE")
        # Best-effort second state check: Google has no atomic free/busy hold.
        with self._repo.write_task(claim.task_id) as tx:
            row = tx.read_outbox(claim.action_hash)
            if (tx.snapshot != context.snapshot or row.status != "LEASED"
                    or row.provider_kind != "google_test"
                    or row.lease_owner != claim.worker_id
                    or row.attempts != 1):
                raise GoogleDispatchDenied("GOOGLE_STALE_EXECUTION_INTENT")
        try:
            # ONE first attempt; a returned 409 must be reconciled by GET.
            self._assert_offline_clients()
            self._service._api.insert_test_event(
                calendar_id=calendar_id, token=token, intent=context.intent,
            )
        except GoogleStateConflict:
            self._assert_offline_clients()
            found = self._service._api.find_effect(
                calendar_id=calendar_id, token=token, intent=context.intent,
            )
            if found is None:
                raise
            return self._verified(claim, context, found, now=now)
        if fault is GoogleWorkerFault.AFTER_INSERT:
            raise InjectedGoogleWorkerCrash("injected exit after provider insert")
        # Never mark complete based on HTTP 200 alone.
        found = self._service._api.find_effect(
            calendar_id=calendar_id, token=token, intent=context.intent,
        )
        if found is None:
            raise GoogleUnknownOutcome("GOOGLE_READBACK_NOT_YET_OBSERVED")
        return self._verified(claim, context, found, now=now)

    def _verified(
        self, claim: OutboxClaim, context: _Dispatch,
        observed: dict[str, object], *, now: datetime,
    ) -> ExecutionReceipt:
        event_ref = stable_event_id(context.intent.action_hash)
        # The HTTP reader already checks status, exact start/end and both private
        # action/request markers. Persist only their safe, canonical digest.
        verification_hash = sha256({
            "action_hash": claim.action_hash,
            "request_hash": context.intent.request_hash,
            "event_ref": event_ref,
            "provider_status": observed.get("status"),
            "provider_start": observed.get("start"),
            "provider_end": observed.get("end"),
            "postcondition": "event_confirmed_exact_match",
        })
        receipt = ExecutionReceipt(
            action_hash=claim.action_hash,
            manifest_hash=claim.manifest_hash,
            outcome=ExecutionOutcome.COMMITTED,
            effect_ref=event_ref,
            verification_ref="verification_" + verification_hash[7:39],
        )
        with self._repo.write_task(claim.task_id) as tx:
            row = tx.read_outbox(claim.action_hash)
            if (row.status != "LEASED" or row.lease_owner != claim.worker_id
                    or row.provider_kind != "google_test"
                    or tx.snapshot.phase is not OfficePhase.EXECUTING):
                raise StateConflict("GOOGLE_VERIFIED_RECEIPT_LEASE_LOST")
            tx.write_execution_outcome(
                claim.action_hash, outcome="VERIFIED",
                receipt_json=receipt.model_dump(mode="json"),
            )
            tx.append(EventKind.ACTION_VERIFIED, payload={
                "proposal_hash": claim.proposal_hash,
                "effect_ref": event_ref,
                "verification_hash": verification_hash,
            }, actor="trusted_google_test_verifier", now=now)
        return receipt

    def _unknown(
        self, claim: OutboxClaim, *, now: datetime, reason: str,
    ) -> ExecutionReceipt:
        if claim.attempts >= self._max_attempts:
            return self._review(claim, now=now, reason="GOOGLE_RECONCILIATION_EXHAUSTED")
        self._repo.release_action_unknown(
            claim, now=now, reason=reason, delay=timedelta(seconds=5),
        )
        return ExecutionReceipt(
            action_hash=claim.action_hash,
            manifest_hash=claim.manifest_hash,
            outcome=ExecutionOutcome.OUTCOME_UNKNOWN,
        )

    def _review(
        self, claim: OutboxClaim, *, now: datetime, reason: str,
    ) -> ExecutionReceipt:
        receipt = ExecutionReceipt(
            action_hash=claim.action_hash,
            manifest_hash=claim.manifest_hash,
            outcome=ExecutionOutcome.OUTCOME_UNKNOWN,
        )
        with self._repo.write_task(claim.task_id) as tx:
            row = tx.read_outbox(claim.action_hash)
            if (row.status != "LEASED" or row.lease_owner != claim.worker_id
                    or row.provider_kind != "google_test"
                    or tx.snapshot.phase is not OfficePhase.EXECUTING):
                raise StateConflict("GOOGLE_REVIEW_LEASE_LOST")
            tx.write_execution_outcome(
                claim.action_hash, outcome="OUTCOME_UNKNOWN",
                receipt_json=receipt.model_dump(mode="json"),
            )
            tx.mark_outbox_terminal(
                claim.action_hash, status="NEEDS_REVIEW",
                reason=reason, owner=claim.worker_id,
            )
            tx.append(EventKind.ACTION_UNVERIFIED, payload={
                "proposal_hash": claim.proposal_hash, "reason_code": reason,
            }, actor="trusted_google_test_reconciler", now=now)
        return receipt
