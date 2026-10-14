"""Opt-in, trusted synthetic recovery supervision; no model or HTTP worker route.

The supervisor makes ONLY bounded calls to the existing SyntheticRecoveryWorker.
That worker alone leases an already admitted synthetic outbox intent, reconciles
unknown outcomes and independently reads back fake-provider effects.

This is a synchronous library building block for a separately hosted worker.
Production process isolation, startup identity and durable service monitoring
remain deployment responsibilities. Never attach this to a model tool registry.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event
from typing import Literal

from pydantic import Field

from safe_agent.contracts.base import StrictModel, require_aware
from safe_agent.contracts.control import ExecutionOutcome, ExecutionReceipt
from safe_agent.execution.recovery import SyntheticRecoveryWorker


class SyntheticSupervisorPolicy(StrictModel):
    schema_version: Literal["safe_agent.synthetic_supervisor_policy.v1"] = (
        "safe_agent.synthetic_supervisor_policy.v1"
    )
    max_claims_per_pass: int = Field(default=5, ge=1, le=50)
    idle_poll_seconds: float = Field(default=1.0, ge=0.01, le=60.0)
    busy_poll_seconds: float = Field(default=0.05, ge=0.01, le=60.0)
    error_backoff_seconds: float = Field(default=2.0, ge=0.05, le=300.0)
    max_consecutive_errors: int = Field(default=3, ge=1, le=10)


class SyntheticWorkerPass(StrictModel):
    """Privacy-minimal counters, not an authority decision or verification receipt."""

    schema_version: Literal["safe_agent.synthetic_worker_pass.v1"] = (
        "safe_agent.synthetic_worker_pass.v1"
    )
    status: Literal["IDLE", "PROCESSED", "ERROR", "HALTED"]
    receipts: int = Field(ge=0)
    verified: int = Field(ge=0)
    unverified: int = Field(ge=0)
    failed: int = Field(ge=0)
    infrastructure_errors: int = Field(ge=0)
    consecutive_errors: int = Field(ge=0)
    exhausted_batch: bool = False


class SyntheticSupervisorReport(StrictModel):
    """Aggregate, sanitized operational summary. NEVER a provider-write grant."""

    schema_version: Literal["safe_agent.synthetic_supervisor_report.v1"] = (
        "safe_agent.synthetic_supervisor_report.v1"
    )
    stop_reason: Literal["STOP_REQUESTED", "PASS_LIMIT", "WORKER_UNHEALTHY"]
    passes: int = Field(ge=0)
    idle_passes: int = Field(ge=0)
    receipts: int = Field(ge=0)
    verified: int = Field(ge=0)
    unverified: int = Field(ge=0)
    failed: int = Field(ge=0)
    infrastructure_errors: int = Field(ge=0)
    synthetic_only: Literal[True] = True
    live_provider_writes_enabled: Literal[False] = False


class SupervisedSyntheticWorker:
    """Bound a synthetic worker's claims and halt after repeated infrastructure errors.

    Never call provider tools, issue approvals or retry individual effects here.
    Releasing an expired lease, deciding whether to reconcile and escalating
    unknown effects remain entirely within SyntheticRecoveryWorker/SQL.
    """

    def __init__(
        self,
        worker: SyntheticRecoveryWorker,
        *,
        policy: SyntheticSupervisorPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(worker) is not SyntheticRecoveryWorker:
            raise TypeError("supervisor requires the exact synthetic recovery worker")
        self._worker = worker
        self._policy = policy or SyntheticSupervisorPolicy()
        self._clock = clock
        self._consecutive_errors = 0
        self._halted = False

    @property
    def halted(self) -> bool:
        """Halted supervisors require operator intervention and a fresh process."""
        return self._halted

    def poll_once(self) -> SyntheticWorkerPass:
        """Process at most one configured batch; no implicit infinite polling."""
        if self._halted:
            return SyntheticWorkerPass(
                status="HALTED", receipts=0, verified=0, unverified=0,
                failed=0, infrastructure_errors=0,
                consecutive_errors=self._consecutive_errors,
            )

        receipts = verified = unverified = failed = errors = 0
        for _ in range(self._policy.max_claims_per_pass):
            # An invalid clock must be rejected before even claiming an intent.
            now = require_aware(self._clock())
            try:
                result = self._worker.process_one(now=now)
                if result is not None and type(result) is not ExecutionReceipt:
                    raise TypeError("unexpected synthetic worker receipt")
            except Exception:
                # Never disclose provider exception details or interpret a
                # possibly committed effect as failed/verified from this loop.
                # Leave all lease/reconciliation decisions to the durable worker.
                errors = 1
                self._consecutive_errors += 1
                if self._consecutive_errors >= self._policy.max_consecutive_errors:
                    self._halted = True
                break

            self._consecutive_errors = 0
            if result is None:
                break
            receipts += 1
            if result.outcome is ExecutionOutcome.COMMITTED:
                verified += 1
            elif result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN:
                # May be either retry-pending or terminal NEEDS_REVIEW. Only
                # persisted task/outbox evidence may distinguish the two.
                unverified += 1
            else:
                failed += 1

        status: Literal["IDLE", "PROCESSED", "ERROR", "HALTED"]
        if self._halted:
            status = "HALTED"
        elif errors:
            status = "ERROR"
        elif receipts:
            status = "PROCESSED"
        else:
            status = "IDLE"
        return SyntheticWorkerPass(
            status=status, receipts=receipts, verified=verified,
            unverified=unverified, failed=failed,
            infrastructure_errors=errors,
            consecutive_errors=self._consecutive_errors,
            exhausted_batch=receipts == self._policy.max_claims_per_pass,
        )

    def serve(
        self, stop_event: Event, *, max_passes: int | None = None,
    ) -> SyntheticSupervisorReport:
        """Poll until stopped, bounded in tests with max_passes.

        The embedding trusted process owns the stop signal and source secrets.
        Event.wait provides interruptible sleeping and avoids a busy-spin
        between unavailable or uncertain-outcome claims.
        """
        if not isinstance(stop_event, Event):
            raise TypeError("a trusted threading.Event is required")
        if max_passes is not None and not 1 <= max_passes <= 10_000:
            raise ValueError("invalid worker pass bound")
        counts = {
            "passes": 0, "idle_passes": 0, "receipts": 0,
            "verified": 0, "unverified": 0, "failed": 0,
            "infrastructure_errors": 0,
        }
        reason: Literal["STOP_REQUESTED", "PASS_LIMIT", "WORKER_UNHEALTHY"] = (
            "STOP_REQUESTED"
        )
        while not stop_event.is_set():
            outcome = self.poll_once()
            counts["passes"] += 1
            counts["idle_passes"] += int(outcome.status == "IDLE")
            counts["receipts"] += outcome.receipts
            counts["verified"] += outcome.verified
            counts["unverified"] += outcome.unverified
            counts["failed"] += outcome.failed
            counts["infrastructure_errors"] += outcome.infrastructure_errors

            if outcome.status == "HALTED":
                reason = "WORKER_UNHEALTHY"
                break
            if stop_event.is_set():
                break
            if max_passes is not None and counts["passes"] >= max_passes:
                reason = "PASS_LIMIT"
                break

            if outcome.status == "ERROR":
                # Cap exponential backoff. No retries are attempted directly;
                # this only paces the next SQL worker claim after an exception.
                backoff = self._policy.error_backoff_seconds * (
                    2 ** min(self._consecutive_errors - 1, 6)
                )
                seconds = min(300.0, backoff)
            elif outcome.exhausted_batch:
                seconds = self._policy.busy_poll_seconds
            else:
                seconds = self._policy.idle_poll_seconds
            if stop_event.wait(seconds):
                break

        return SyntheticSupervisorReport(
            stop_reason=reason,
            passes=counts["passes"], idle_passes=counts["idle_passes"],
            receipts=counts["receipts"], verified=counts["verified"],
            unverified=counts["unverified"], failed=counts["failed"],
            infrastructure_errors=counts["infrastructure_errors"],
        )
