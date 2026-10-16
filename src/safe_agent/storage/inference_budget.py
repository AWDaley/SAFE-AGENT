"""Transactional inference cost reservations, safe holds and sealed reconciliation.

This ledger enforces cost *capacity*, not proof of vendor billing. An unknown
provider result is held at the full reserved ceiling until an independently
hosted billing verifier supplies an HMAC-attested charge. Model usage reports
NEVER release funds. All mutations are in trusted SQL task transactions.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, update

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import require_aware
from safe_agent.contracts.billing_verification import VerifiedBillingAttestation
from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.contracts.inference import InferenceReportedUsage, InferenceRequest
from safe_agent.contracts.inference_budget import (
    BillingChargeAttestation,
    InferenceAccountBalance,
    InferenceBudgetLimits,
    InferenceBudgetView,
    InferenceReservationReceipt,
)
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.security.principal import Principal
from safe_agent.storage.models import (
    InferenceBillingAttestationRow,
    InferenceBudgetAccountRow,
    InferenceBudgetEventRow,
    InferenceBudgetReservationRow,
)
from safe_agent.storage.repository import LedgerRepository, StateConflict, TaskTransaction

BudgetReason = Literal["UNKNOWN_EFFECT", "UNVERIFIED_RESULT", "LEASE_EXPIRED"]
_ALLOWED_REASON = {"UNKNOWN_EFFECT", "UNVERIFIED_RESULT", "LEASE_EXPIRED"}
_FINAL = {"RECONCILED", "SYNTHETIC_SETTLED"}


class InferenceBudgetDenied(ValueError):
    """Fixed, safe error: cannot expose another tenant's spending, credentials or SQL."""

    def __init__(self) -> None:
        super().__init__("INFERENCE_BUDGET_DENIED")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _keys(organization_id: str, task_id: str) -> tuple[tuple[str, str], ...]:
    return (
        ("GLOBAL", "global"),
        ("TENANT", "tenant_" + sha256({"org": organization_id})[7:]),
        ("TASK", "task_" + sha256({
            "org": organization_id, "task": task_id,
        })[7:]),
    )


def _limits(limits: InferenceBudgetLimits) -> tuple[int, int, int]:
    return (limits.global_microusd, limits.tenant_microusd, limits.task_microusd)


def _event_data(
    row: InferenceBudgetReservationRow, *,
    sequence: int, kind: str, held: int, charged: int,
    evidence_hash: str | None, reason: str | None, occurred_at: datetime,
    preceding_hash: str | None,
) -> dict[str, object]:
    return {
        "request_id": row.request_id, "organization_id": row.organization_id,
        "task_id": row.task_id, "route_id": row.route_id,
        "model_revision": row.model_revision, "prompt_digest": row.prompt_digest,
        "backend_kind": row.backend_kind, "sequence": sequence, "event_kind": kind,
        "held_microusd": held, "charged_microusd": charged,
        "evidence_hash": evidence_hash, "reason_code": reason,
        "occurred_at": _utc(occurred_at), "preceding_hash": preceding_hash,
    }


def _append_event(
    tx: TaskTransaction, row: InferenceBudgetReservationRow,
    *, kind: str, at: datetime, reason: str | None = None,
) -> None:
    previous = row.latest_event_hash if row.event_sequence else None
    sequence = row.event_sequence + 1
    payload = _event_data(
        row, sequence=sequence, kind=kind,
        held=row.held_microusd, charged=row.charged_microusd,
        evidence_hash=row.evidence_hash, reason=reason,
        occurred_at=at, preceding_hash=previous,
    )
    digest = sha256(payload)
    tx.session.add(InferenceBudgetEventRow(
        request_id=row.request_id, sequence=sequence,
        event_kind=kind, held_microusd=row.held_microusd,
        charged_microusd=row.charged_microusd,
        evidence_hash=row.evidence_hash, reason_code=reason,
        occurred_at=at, preceding_hash=previous, event_hash=digest,
    ))
    row.event_sequence = sequence
    row.latest_event_hash = digest
    row.updated_at = at
    tx.session.flush()


def _audit_events(tx: TaskTransaction, row: InferenceBudgetReservationRow) -> None:
    events = tx.session.scalars(
        select(InferenceBudgetEventRow)
        .where(InferenceBudgetEventRow.request_id == row.request_id)
        .order_by(InferenceBudgetEventRow.sequence)
    ).all()
    if len(events) != row.event_sequence or not events:
        raise StateConflict("inference budget evidence sequence mismatch")
    preceding: str | None = None
    for i, event in enumerate(events, start=1):
        payload = _event_data(
            row, sequence=i, kind=event.event_kind,
            held=event.held_microusd, charged=event.charged_microusd,
            evidence_hash=event.evidence_hash, reason=event.reason_code,
            occurred_at=_utc(event.occurred_at),
            preceding_hash=preceding,
        )
        if (event.sequence != i or event.preceding_hash != preceding
                or sha256(payload) != event.event_hash):
            raise StateConflict("inference budget event integrity failure")
        preceding = event.event_hash
    if (preceding != row.latest_event_hash
            or row.held_microusd != events[-1].held_microusd
            or row.charged_microusd != events[-1].charged_microusd
            or row.evidence_hash != events[-1].evidence_hash
            or row.status != {
                "RESERVED": "IN_FLIGHT",
                "UNCERTAIN": "UNCERTAIN",
                "SYNTHETIC_SETTLED": "SYNTHETIC_SETTLED",
                "ATTESTED": "RECONCILED",
            }.get(events[-1].event_kind)
            or row.held_microusd + row.charged_microusd > row.max_microusd):
        raise StateConflict("inference budget terminal event mismatch")


def _receipt(row: InferenceBudgetReservationRow) -> InferenceReservationReceipt:
    return InferenceReservationReceipt.model_validate({
        "request_id": row.request_id, "task_id": row.task_id,
        "status": row.status,
        "held_microusd": row.held_microusd,
        "charged_microusd": row.charged_microusd,
        "max_estimated_microusd": row.max_microusd,
        "evidence_hash": row.evidence_hash,
        "ledger_hash": row.latest_event_hash,
    })


def _ensure_account(
    tx: TaskTransaction, key: str, kind: str, limit: int,
) -> InferenceBudgetAccountRow:
    row = tx.session.scalar(
        select(InferenceBudgetAccountRow)
        .where(InferenceBudgetAccountRow.scope_key == key).with_for_update()
    )
    if row is None:
        row = InferenceBudgetAccountRow(
            scope_key=key, kind=kind, limit_microusd=limit,
            held_microusd=0, charged_microusd=0, version=0,
        )
        tx.session.add(row)
        tx.session.flush()
    if (row.kind != kind or row.limit_microusd != limit
            or row.held_microusd < 0 or row.charged_microusd < 0
            or row.held_microusd + row.charged_microusd > limit):
        raise StateConflict("inference budget account authority mismatch")
    return row


def _change_account(
    tx: TaskTransaction, key: str, *, hold_delta: int, charge_delta: int,
) -> None:
    # Conditional SQL UPDATE is atomic even under simultaneous tenant tasks;
    # never rely on SELECT/modify/flush in a SQLite test or multi-worker host.
    table = InferenceBudgetAccountRow
    result = tx.session.connection().execute(
        update(table)
        .where(
            table.scope_key == key,
            table.held_microusd + hold_delta >= 0,
            table.charged_microusd + charge_delta >= 0,
            table.held_microusd + table.charged_microusd
            + hold_delta + charge_delta <= table.limit_microusd,
        )
        .values(
            held_microusd=table.held_microusd + hold_delta,
            charged_microusd=table.charged_microusd + charge_delta,
            version=table.version + 1,
        )
    )
    if result.rowcount != 1:
        raise InferenceBudgetDenied()


def _assert_task_authority(tx: TaskTransaction, request: InferenceRequest) -> None:
    task, manifest, snapshot = tx.task, tx.manifest, tx.snapshot
    context = request.task
    if (not isinstance(task, Task) or not isinstance(manifest, TaskManifest)
            or task.task_id != context.task_id
            or task.organization_id != context.organization_id
            or manifest.initiator_id != task.initiator_id
            or snapshot.phase is not OfficePhase.ASSIGNED
            or snapshot.proposal_hash is not None
            or task.manifest_hash != snapshot.manifest_hash
            or manifest.permitted_operations != ("calendar.create_event",)
            or manifest.calendar_ref != context.calendar_ref
            or manifest.customer_ref != context.customer_ref
            or manifest.template_id != context.template_id
            or manifest.time_zone != context.time_zone
            or manifest.allowed_start != context.allowed_start
            or manifest.allowed_end != context.allowed_end
            or context.duration_minutes > manifest.max_duration_minutes
            or request.issued_at >= task.deadline):
        raise InferenceBudgetDenied()


class InferenceBudgetLedger:
    """Durable global + tenant + task cost capacity with conservative holds.

    No method can create a calendar effect or authenticate a model. Exact
    trusted ceilings are write-once. A second service with different ceilings
    must not silently increase an existing account's spending authority.
    """

    def __init__(self, repo: LedgerRepository, limits: InferenceBudgetLimits) -> None:
        self._repo = repo
        self._limits = limits

    def reserve(
        self, request: InferenceRequest, *, max_microusd: int,
        synthetic_mock: bool, now: datetime,
    ) -> InferenceReservationReceipt:
        # Preserve the original fail-closed API: repeated request IDs are denied.
        with self._repo.write_task(request.task.task_id) as tx:
            return self.reserve_in_transaction(
                tx, request, max_microusd=max_microusd,
                synthetic_mock=synthetic_mock, now=now,
            )

    def reserve_in_transaction(
        self, tx: TaskTransaction, request: InferenceRequest, *, max_microusd: int,
        synthetic_mock: bool, now: datetime,
    ) -> InferenceReservationReceipt:
        """Trusted caller owns commit/rollback; never open or commit a second session.

        Requires the repository's current task transaction. This is not a model
        API and does not grant permission to invoke a transport or refund a hold.
        """
        if (tx.session.get_bind() is not self._repo.engine
                or not tx.session.in_transaction()
                or tx.row.task_id != request.task.task_id):
            raise InferenceBudgetDenied()
        now = require_aware(now).astimezone(UTC)
        if (type(synthetic_mock) is not bool or type(max_microusd) is not int
                or not 0 <= max_microusd <= self._limits.max_call_microusd):
            raise InferenceBudgetDenied()
        try:
            request = InferenceRequest.model_validate(request.model_dump())
        except (TypeError, ValueError):
            raise InferenceBudgetDenied() from None
        if (now < request.issued_at or now >= request.deadline_at
                or now >= request.availability.expires_at):
            raise InferenceBudgetDenied()
        _assert_task_authority(tx, request)
        if tx.session.get(InferenceBudgetReservationRow, request.request_id) is not None:
            # Replaying a reservation is NOT permission to dispatch twice.
            raise InferenceBudgetDenied()
        scope = _keys(request.task.organization_id, request.task.task_id)
        for (kind, key), limit in zip(scope, _limits(self._limits), strict=True):
            _ensure_account(tx, key, kind, limit)
        for _, key in scope:
            _change_account(tx, key, hold_delta=max_microusd, charge_delta=0)
        row = InferenceBudgetReservationRow(
            request_id=request.request_id,
            organization_id=request.task.organization_id,
            task_id=request.task.task_id,
            route_id=request.route_id, model_revision=request.model_revision,
            prompt_digest=request.prompt_digest,
            backend_kind="MOCK" if synthetic_mock else "REMOTE",
            max_microusd=max_microusd, held_microusd=max_microusd,
            charged_microusd=0, status="IN_FLIGHT",
            reserved_at=now, deadline_at=request.deadline_at,
            reported_input_tokens=None, reported_output_tokens=None,
            evidence_hash=None, event_sequence=0, latest_event_hash="",
            updated_at=now,
        )
        # Distinct status for zero-cost, genuine in-process mock. The model
        # cannot nominate this mode; trusted bootstrap selects the backend.
        tx.session.add(row)
        tx.session.flush()
        _append_event(tx, row, kind="RESERVED", at=now)
        # Avoid a new separate column; mock proof remains callable only
        # through the exact type-checked gateway.
        return _receipt(row)

    def mark_uncertain(
        self, request_id: str, *, now: datetime,
        reason: BudgetReason = "UNKNOWN_EFFECT",
        reported_usage: InferenceReportedUsage | None = None,
    ) -> InferenceReservationReceipt:
        now = require_aware(now).astimezone(UTC)
        if reason not in _ALLOWED_REASON:
            raise InferenceBudgetDenied()
        # Read only the exact request ID; do not accept a guessed task binding.
        with self._repo._sessions() as s:
            found = s.get(InferenceBudgetReservationRow, request_id)
            if found is None:
                raise InferenceBudgetDenied()
            task_id = found.task_id
        with self._repo.write_task(task_id) as tx:
            row = tx.session.get(InferenceBudgetReservationRow, request_id)
            if row is None or row.task_id != task_id:
                raise InferenceBudgetDenied()
            _audit_events(tx, row)
            if row.status == "UNCERTAIN":
                return _receipt(row)
            if row.status != "IN_FLIGHT" or now < _utc(row.reserved_at):
                raise InferenceBudgetDenied()
            if reported_usage is not None:
                reported_usage = InferenceReportedUsage.model_validate_json(
                    reported_usage.model_dump_json()
                )
                row.reported_input_tokens = reported_usage.input_tokens
                row.reported_output_tokens = reported_usage.output_tokens
            row.status = "UNCERTAIN"
            _append_event(tx, row, kind="UNCERTAIN", at=now, reason=reason)
            # Full reservation is retained, even after timeout or a valid
            # model proposal, because billing has not been independently read.
            return _receipt(row)

    def close_synthetic(
        self, request_id: str, *, now: datetime,
        exact_offline_mock: bool,
    ) -> InferenceReservationReceipt:
        now = require_aware(now).astimezone(UTC)
        if exact_offline_mock is not True:
            raise InferenceBudgetDenied()
        with self._repo._sessions() as s:
            found = s.get(InferenceBudgetReservationRow, request_id)
            if found is None:
                raise InferenceBudgetDenied()
            task_id = found.task_id
        with self._repo.write_task(task_id) as tx:
            row = tx.session.get(InferenceBudgetReservationRow, request_id)
            if (row is None or row.status != "IN_FLIGHT"
                    or row.backend_kind != "MOCK"):
                raise InferenceBudgetDenied()
            _audit_events(tx, row)
            if now < _utc(row.reserved_at):
                raise InferenceBudgetDenied()
            hold = row.held_microusd
            for _, key in _keys(row.organization_id, row.task_id):
                _change_account(tx, key, hold_delta=-hold, charge_delta=0)
            row.held_microusd = 0
            row.status = "SYNTHETIC_SETTLED"
            _append_event(tx, row, kind="SYNTHETIC_SETTLED", at=now)
            return _receipt(row)

    def _reconcile_authenticated(
        self, *, receipt_id: str, request_id: str, organization_id: str,
        task_id: str, route_id: str, model_revision: str, prompt_digest: str,
        charge_microusd: int, source_evidence_hash: str, issuer_id: str,
        payload_hash: str, observed_at: datetime, now: datetime,
    ) -> InferenceReservationReceipt:
        if now < observed_at:
            raise InferenceBudgetDenied()
        with self._repo._sessions() as s:
            found = s.get(InferenceBudgetReservationRow, request_id)
            if found is None:
                raise InferenceBudgetDenied()
            stored_task_id = found.task_id
        with self._repo.write_task(stored_task_id) as tx:
            row = tx.session.get(InferenceBudgetReservationRow, request_id)
            if row is None or row.status not in ("IN_FLIGHT", "UNCERTAIN"):
                raise InferenceBudgetDenied()
            _audit_events(tx, row)
            if (row.organization_id != organization_id
                    or row.task_id != task_id
                    or row.route_id != route_id
                    or row.model_revision != model_revision
                    or row.prompt_digest != prompt_digest
                    or charge_microusd > row.max_microusd
                    or tx.session.get(InferenceBillingAttestationRow, receipt_id) is not None):
                # Over-reservation or binding disagreement requires incident
                # review; never release capacity or invent a capped charge.
                raise InferenceBudgetDenied()
            hold = row.held_microusd
            for _, key in _keys(row.organization_id, row.task_id):
                _change_account(
                    tx, key, hold_delta=-hold,
                    charge_delta=charge_microusd,
                )
            row.held_microusd = 0
            row.charged_microusd = charge_microusd
            row.status = "RECONCILED"
            row.evidence_hash = payload_hash
            tx.session.add(InferenceBillingAttestationRow(
                receipt_id=receipt_id, request_id=row.request_id,
                issuer_id=issuer_id, charge_microusd=charge_microusd,
                source_evidence_hash=source_evidence_hash,
                attestation_digest=payload_hash, accepted_at=now,
            ))
            _append_event(tx, row, kind="ATTESTED", at=now)
            return _receipt(row)

    def reconcile_attested(
        self, evidence: BillingChargeAttestation, *,
        verifier_secret: bytes, now: datetime,
    ) -> InferenceReservationReceipt:
        """Legacy HMAC fixture path retained for backward-compatible tests."""
        now = require_aware(now).astimezone(UTC)
        if type(verifier_secret) is not bytes or len(verifier_secret) < 32:
            raise InferenceBudgetDenied()
        payload = evidence.model_dump(mode="json", exclude={"signature"})
        mac = hmac.new(
            verifier_secret, canonical_json(payload), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(mac, evidence.signature):
            raise InferenceBudgetDenied()
        return self._reconcile_authenticated(
            receipt_id=evidence.receipt_id, request_id=evidence.request_id,
            organization_id=evidence.organization_id, task_id=evidence.task_id,
            route_id=evidence.route_id, model_revision=evidence.model_revision,
            prompt_digest=evidence.prompt_digest,
            charge_microusd=evidence.charge_microusd,
            source_evidence_hash=evidence.source_evidence_hash,
            issuer_id=evidence.issuer_id, payload_hash=sha256(payload),
            observed_at=evidence.observed_at, now=now,
        )

    def reconcile_verified(
        self, evidence: VerifiedBillingAttestation, *,
        verifier_public_key: bytes, expected_issuer_id: str,
        expected_verifier_key_id: str, now: datetime,
    ) -> InferenceReservationReceipt:
        """Apply evidence signed by an isolated verifier private key.

        SAFE-AGENT needs only the public key. Possessing this verification
        capability cannot mint a valid attestation.
        """
        now = require_aware(now).astimezone(UTC)
        if (type(verifier_public_key) is not bytes or len(verifier_public_key) != 32
                or evidence.issuer_id != expected_issuer_id
                or evidence.verifier_key_id != expected_verifier_key_id):
            raise InferenceBudgetDenied()
        try:
            checked = VerifiedBillingAttestation.model_validate_json(
                evidence.model_dump_json(),
            )
            payload = checked.model_dump(mode="json", exclude={"signature"})
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(verifier_public_key).verify(
                bytes.fromhex(checked.signature), canonical_json(payload),
            )
        except Exception:
            raise InferenceBudgetDenied() from None
        return self._reconcile_authenticated(
            receipt_id=checked.receipt_id, request_id=checked.request_id,
            organization_id=checked.organization_id, task_id=checked.task_id,
            route_id=checked.route_id, model_revision=checked.model_revision,
            prompt_digest=checked.prompt_digest,
            charge_microusd=checked.charge_microusd,
            source_evidence_hash=checked.source_evidence_hash,
            issuer_id=checked.issuer_id, payload_hash=sha256(payload),
            observed_at=checked.observed_at, now=now,
        )

    def mark_expired_uncertain(
        self, *, now: datetime, max_scan: int = 100,
    ) -> int:
        """Optional trusted recovery; expirations NEVER refund held capacity."""
        now = require_aware(now).astimezone(UTC)
        if not 1 <= max_scan <= 1000:
            raise ValueError("scan bound 1..1000")
        with self._repo._sessions() as s:
            ids = s.scalars(
                select(InferenceBudgetReservationRow.request_id)
                .where(InferenceBudgetReservationRow.status == "IN_FLIGHT")
                .order_by(InferenceBudgetReservationRow.deadline_at)
                .limit(max_scan)
            ).all()
        changed = 0
        for req_id in ids:
            with self._repo._sessions() as s:
                row = s.get(InferenceBudgetReservationRow, req_id)
                if row is None or _utc(row.deadline_at) > now:
                    continue
            receipt = self.mark_uncertain(
                req_id, now=now, reason="LEASE_EXPIRED",
            )
            changed += int(receipt.status == "UNCERTAIN")
        return changed

    def receipt(
        self, principal: Principal, request_id: str, *, now: datetime,
    ) -> InferenceReservationReceipt:
        now = require_aware(now)
        with self._repo._sessions() as s:
            found = s.get(InferenceBudgetReservationRow, request_id)
            if found is None:
                raise InferenceBudgetDenied()
            task_id = found.task_id
        with self._repo.write_task(task_id) as tx:
            principal.require("task:read", tx.task.organization_id, now)
            if principal.actor_id != tx.task.initiator_id:
                raise InferenceBudgetDenied()
            row = tx.session.get(InferenceBudgetReservationRow, request_id)
            if row is None or row.organization_id != tx.task.organization_id:
                raise InferenceBudgetDenied()
            _audit_events(tx, row)
            return _receipt(row)

    def view(
        self, principal: Principal, task_id: str, *, now: datetime,
    ) -> InferenceBudgetView:
        now = require_aware(now)
        with self._repo.write_task(task_id) as tx:
            principal.require("task:read", tx.task.organization_id, now)
            if principal.actor_id != tx.task.initiator_id:
                raise InferenceBudgetDenied()
            keys = _keys(tx.task.organization_id, task_id)
            balances: dict[str, InferenceAccountBalance] = {}
            for (kind, key), limit in zip(keys, _limits(self._limits), strict=True):
                row = _ensure_account(tx, key, kind, limit)
                balances[kind] = InferenceAccountBalance.model_validate({
                    "scope": kind, "limit_microusd": limit,
                    "held_microusd": row.held_microusd,
                    "charged_microusd": row.charged_microusd,
                    "available_microusd": (
                        limit - row.held_microusd - row.charged_microusd
                    ),
                    "version": row.version,
                })
            return InferenceBudgetView(
                task_id=task_id, tenant=balances["TENANT"],
                task=balances["TASK"],
                global_budget_enforced=True,
            )
