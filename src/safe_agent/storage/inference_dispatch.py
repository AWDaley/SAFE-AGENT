"""M7-05c trusted claims, one-way dispatch and conservative recovery.

All transitions use the existing task lock and admission authority. No network
runs inside a transaction. A claim is internal metadata, never a bearer tool.
W3 persists a closed validated result in the SAME successful finish transaction.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from pydantic import SecretStr
from sqlalchemy import select

from safe_agent.canonical import sha256
from safe_agent.contracts.actions import parse_proposal_json
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.inference import (
    InferenceOutcome,
    InferenceRequest,
    InferenceTransportReply,
)
from safe_agent.contracts.inference_admission import InferenceAdmissionReceipt
from safe_agent.contracts.inference_coordinator import InferenceDispatchClaim
from safe_agent.contracts.inference_dispatch import (
    InferenceInvocationEvent,
    InferenceInvocationView,
    InvocationReason,
    InvocationState,
    seal_invocation_event,
)
from safe_agent.contracts.planning import PlanningLease
from safe_agent.inference.gateway import _check_candidate, _div_ceiling, _prompt_models
from safe_agent.security.principal import Principal
from safe_agent.storage.inference_admission import (
    TrustedInferenceAdmission,
    _history,
    _live_attempt,
    _task_identity,
)
from safe_agent.storage.inference_budget import _append_event as append_budget_event
from safe_agent.storage.inference_results import persist_validated_result
from safe_agent.storage.models import (
    InferenceBudgetReservationRow,
    InferenceInvocationEventRow,
    InferenceInvocationRow,
)
from safe_agent.storage.planning import _utc
from safe_agent.storage.repository import TaskTransaction


class InferenceDispatchDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("INFERENCE_DISPATCH_DENIED")


@dataclass(frozen=True)
class _Locked:
    tx: TaskTransaction
    row: InferenceInvocationRow
    receipt: InferenceAdmissionReceipt
    request: InferenceRequest
    now: datetime


def _event(row: InferenceInvocationEventRow) -> InferenceInvocationEvent:
    data = {field: getattr(row, field) for field in InferenceInvocationEvent.model_fields
            if field != "schema_version"}
    data["occurred_at"] = _utc(row.occurred_at)
    return InferenceInvocationEvent.model_validate(data)


def _token_digest(token: SecretStr) -> str:
    return hashlib.sha256(token.get_secret_value().encode("ascii")).hexdigest()


class TrustedInferenceDispatchStore:
    """A single task lock protects every claim/start/finish/recovery decision."""
    def __init__(self, admission: TrustedInferenceAdmission, *, claim_seconds: int = 5) -> None:
        if type(admission) is not TrustedInferenceAdmission:
            raise TypeError("exact trusted admission service required")
        if type(claim_seconds) is not int or not 1 <= claim_seconds <= 30:
            raise ValueError("claim bound 1..30 required")
        self.admission = admission
        self._claim_seconds = claim_seconds

    @contextmanager
    def _locked(self, principal: Principal, lease: PlanningLease, prompt: str) -> Iterator[_Locked]:
        try:
            actor = Principal.model_validate(principal.model_dump())
            planning = PlanningLease.model_validate(lease.model_dump())
            service = self.admission
            policy = service._policy.route
            context, projection, data = _prompt_models(prompt, max_bytes=min(
                policy.max_prompt_bytes, policy.max_prompt_token_proxy,
            ))
            upper = (_div_ceiling(len(data) * policy.input_microusd_per_million_tokens, 1_000_000)
                     + _div_ceiling(min(policy.max_response_bytes, policy.max_response_token_proxy)
                                    * policy.output_microusd_per_million_tokens, 1_000_000))
            with service._repo.write_task(planning.task_id, serialize_sqlite=True) as tx:
                _task_identity(tx, actor, planning, context, require_aware(service._clock()))
                authority = service._authority(tx)
                now = require_aware(service._clock()).astimezone(UTC)
                _task_identity(tx, actor, planning, context, now)
                if now < _utc(authority.last_seen_at):
                    raise InferenceDispatchDenied()
                authority.last_seen_at = now
                row = tx.session.scalar(select(InferenceInvocationRow).where(
                    InferenceInvocationRow.organization_id == actor.organization_id,
                    InferenceInvocationRow.task_id == planning.task_id,
                    InferenceInvocationRow.attempt_id == planning.attempt_id,
                ))
                if row is None:
                    raise InferenceDispatchDenied()
                intent = sha256({"policy_hash": service._policy_hash, "lease": planning,
                                 "prompt_hash": sha256(data.decode("utf-8")), "max_microusd": upper})
                receipt = service._existing(tx, row, planning, context, projection, intent, upper)
                bound = receipt.binding
                if now < _utc(row.updated_at):
                    raise InferenceDispatchDenied()
                request = service._request(context, projection, request_id=bound.request_id,
                                           issued_at=bound.issued_at, deadline_at=bound.deadline_at)
                yield _Locked(tx, row, receipt, request, now)
        except Exception:
            raise InferenceDispatchDenied() from None

    def _transition(self, locked: _Locked, target: InvocationState, *,
                    reason: InvocationReason | None = None,
                    proposal_hash: str | None = None) -> InferenceInvocationView:
        tx, row, bound = locked.tx, locked.row, locked.receipt.binding
        previous = tx.session.get(InferenceInvocationEventRow, (row.request_id, row.event_sequence))
        if previous is None:
            raise InferenceDispatchDenied()
        event = seal_invocation_event(bound, state=target, occurred_at=locked.now,
                                      previous=_event(previous), reason_code=reason,
                                      proposal_hash=proposal_hash)
        row.state = target
        row.state_version += 1
        row.event_sequence = event.sequence
        row.latest_event_hash = event.event_hash
        row.updated_at = locked.now
        row.reason_code = reason
        row.proposal_hash = proposal_hash
        if target == "DISPATCH_STARTED":
            row.dispatch_started_at = locked.now
        tx.session.flush()
        tx.session.add(InferenceInvocationEventRow(**event.model_dump(exclude={"schema_version"})))
        reserve = tx.session.get(InferenceBudgetReservationRow, row.request_id)
        if reserve is None:
            raise InferenceDispatchDenied()
        # Canonical cost event, in the same transaction. Never alter any amount
        # or authenticate a model-reported invoice; remote-bound holds remain.
        if reserve.status == "IN_FLIGHT":
            reserve.status = "UNCERTAIN"
            append_budget_event(tx, reserve, kind="UNCERTAIN", at=locked.now,
                                reason="UNKNOWN_EFFECT")
        tx.session.flush()
        return _history(tx, row, bound)

    def _live(self, locked: _Locked, lease: PlanningLease) -> None:
        _live_attempt(locked.tx, lease, locked.now)
        if not locked.receipt.binding.issued_at <= locked.now < locked.receipt.binding.deadline_at:
            raise InferenceDispatchDenied()

    def _fresh_commit(self, locked: _Locked) -> None:
        # Slow SQL must not create a fresh successful claim/start after expiry.
        end = require_aware(self.admission._clock()).astimezone(UTC)
        if not locked.now <= end < locked.receipt.binding.deadline_at:
            raise InferenceDispatchDenied()

    def claim(self, principal: Principal, lease: PlanningLease,
              prompt: str) -> InferenceDispatchClaim | None:
        with self._locked(principal, lease, prompt) as locked:
            row, now = locked.row, locked.now
            if row.state != "RESERVED":
                return None
            self._live(locked, lease)
            if row.claim_owner is not None and row.claim_until is not None and now < _utc(row.claim_until):
                raise InferenceDispatchDenied()
            token = SecretStr(token_hex(32))
            row.claim_owner = _token_digest(token)
            row.claim_version += 1
            row.claim_until = min(now + timedelta(seconds=self._claim_seconds),
                                  locked.receipt.binding.deadline_at)
            row.updated_at = now
            locked.tx.session.flush()
            self._fresh_commit(locked)
            return InferenceDispatchClaim(binding=locked.receipt.binding,
                                          claim_version=row.claim_version,
                                          claim_until=_utc(row.claim_until), token=token)

    @staticmethod
    def _owns(locked: _Locked, claim: InferenceDispatchClaim) -> None:
        # Explicit token copy: model_dump deliberately excludes this credential.
        checked = InferenceDispatchClaim.model_validate({**claim.model_dump(), "token": claim.token})
        row = locked.row
        if (checked.binding != locked.receipt.binding or row.claim_version != checked.claim_version
                or row.claim_until is None or _utc(row.claim_until) != checked.claim_until
                or row.claim_owner is None
                or not hmac.compare_digest(row.claim_owner, _token_digest(checked.token))):
            raise InferenceDispatchDenied()

    def start(self, principal: Principal, lease: PlanningLease, prompt: str,
              claim: InferenceDispatchClaim) -> tuple[InferenceRequest, str]:
        with self._locked(principal, lease, prompt) as locked:
            self._owns(locked, claim)
            self._live(locked, lease)
            if locked.row.state != "RESERVED" or locked.now >= claim.claim_until:
                raise InferenceDispatchDenied()
            if locked.receipt.reservation.status != "IN_FLIGHT":
                raise InferenceDispatchDenied()
            view = self._transition(locked, "DISPATCH_STARTED")
            self._fresh_commit(locked)
            return locked.request, view.latest_event_hash

    def finish(self, principal: Principal, lease: PlanningLease, prompt: str,
               claim: InferenceDispatchClaim, reply: InferenceTransportReply) -> tuple[InferenceInvocationView, InferenceOutcome]:
        # Revalidate before any model output is persisted or returned.
        with self._locked(principal, lease, prompt) as locked:
            checked = InferenceTransportReply.model_validate(reply.model_dump())
            self._owns(locked, claim)
            self._live(locked, lease)
            req, pol = locked.request, self.admission._policy.route
            if (locked.row.state != "DISPATCH_STARTED"
                    or checked.request_id != req.request_id or checked.route_id != req.route_id
                    or checked.model_revision != req.model_revision or checked.prompt_digest != req.prompt_digest
                    or len(checked.output_json.encode("utf-8")) > min(pol.max_response_bytes, pol.max_response_token_proxy)
                    or (checked.reported_usage is not None and (
                        checked.reported_usage.input_tokens > pol.max_prompt_token_proxy
                        or checked.reported_usage.output_tokens > pol.max_response_token_proxy))):
                raise InferenceDispatchDenied()
            candidate = _check_candidate(parse_proposal_json(checked.output_json), req.task, req.availability)
            outcome = InferenceOutcome(request_id=req.request_id, route_id=req.route_id,
                                       model_revision=req.model_revision, prompt_digest=req.prompt_digest,
                                       proposal_digest=sha256(candidate), proposal=candidate,
                                       reported_usage=checked.reported_usage)
            view = self._transition(locked, "RESPONSE_VALIDATED", proposal_hash=outcome.proposal_digest)
            # Result, validation event and invocation transition either ALL commit
            # or ALL roll back. A lost response is resolved by reading, not retrying.
            persist_validated_result(
                locked.tx, binding=locked.receipt.binding, lease=lease,
                policy_hash=self.admission._policy_hash, request=req, outcome=outcome,
                view=view, now=locked.now,
            )
            self._fresh_commit(locked)
            return view, outcome

    def unknown(self, principal: Principal, lease: PlanningLease, prompt: str,
                claim: InferenceDispatchClaim, *, rejected: bool = False) -> InferenceInvocationView:
        with self._locked(principal, lease, prompt) as locked:
            self._owns(locked, claim)
            if locked.row.state == "UNKNOWN":
                return locked.receipt.invocation
            if locked.row.state != "DISPATCH_STARTED":
                raise InferenceDispatchDenied()
            return self._transition(locked, "UNKNOWN", reason=(
                "RESPONSE_REJECTED" if rejected else "DISPATCH_OUTCOME_UNKNOWN"
            ))

    def cancel_before_dispatch(
        self, principal: Principal, lease: PlanningLease, prompt: str,
    ) -> InferenceInvocationView:
        """Close an admitted request only while no dispatch marker exists.

        The remote-bound budget hold remains conservative. This changes
        admission liveness, not billing evidence, and never calls a transport.
        """
        with self._locked(principal, lease, prompt) as locked:
            if locked.row.state == "FAILED_BEFORE_DISPATCH":
                return locked.receipt.invocation
            if locked.row.state != "RESERVED":
                raise InferenceDispatchDenied()
            reason: InvocationReason = (
                "AUTHORITY_EXPIRED"
                if locked.now >= locked.receipt.binding.deadline_at
                else "CANCELLED_BEFORE_DISPATCH"
            )
            return self._transition(locked, "FAILED_BEFORE_DISPATCH", reason=reason)

    def recover(self, principal: Principal, lease: PlanningLease, prompt: str) -> InferenceInvocationView:
        """Trusted, request-scoped recovery. Never reclaims a started call or refunds."""
        with self._locked(principal, lease, prompt) as locked:
            if locked.now >= locked.receipt.binding.deadline_at:
                if locked.row.state == "RESERVED":
                    return self._transition(locked, "FAILED_BEFORE_DISPATCH", reason="AUTHORITY_EXPIRED")
                if locked.row.state == "DISPATCH_STARTED":
                    return self._transition(locked, "UNKNOWN", reason="DISPATCH_OUTCOME_UNKNOWN")
            return locked.receipt.invocation
