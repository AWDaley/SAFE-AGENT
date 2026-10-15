# M6-01d — Supervised synthetic outbox worker

**Status:** Opt-in trusted local/offline supervisor library and demonstration.
**Dependency:** PR #22 stacks on PR #21 (which stacks on #20). Review and merge
in order. **M5 LIVE ACCEPTANCE: BLOCKED.**

## Scope

The SupervisedSyntheticWorker in src/safe_agent/execution/supervisor.py
is a trusted polling loop for the existing SyntheticRecoveryWorker. It
does not introduce a model tool, FastAPI worker endpoint, provider client,
new TAL gate or task permission system.

Only SyntheticRecoveryWorker.process_one(now=...) can claim work. That worker
already owns the durable SQL lease, conservative unknown-result reconciliation,
provider-owned fake idempotency and independent readback. Its exact fake-adapter
and provider_kind=synthetic fences remain unchanged.

The supervisor adds bounded claims per pass (1–50), an explicit one-pass method,
an operator-owned threading.Event to stop interruptible polling, a finite pass
bound for acceptance tests, configurable idle and busy pauses, bounded
exponential backoff for unexpected worker/SQL errors, and an unhealthy terminal
state after repeated failures. Its output contains aggregate counts only;
no task/customer/calendar IDs, provider IDs, tokens or exception strings.

It does not turn local READY_TO_COMMIT into authorization. Only *already
queued* synthetic intents are eligible for its worker to claim.

## Trusted host wiring

Reuse the same LedgerRepository, durable ReferenceBroker signing key,
VirtualOffice, DurableFakeProvider, exact fake adapters and
SyntheticRecoveryWorker described in M6_SYNTHETIC_WORKFLOW.md. The HTTP
queue-synthetic endpoint persists the intent; it never calls the supervisor.

In an operator-controlled worker host, construct the supervisor explicitly:

    from threading import Event
    from safe_agent.execution.supervisor import (
        SupervisedSyntheticWorker, SyntheticSupervisorPolicy,
    )

    stop = Event()
    service = SupervisedSyntheticWorker(
        worker,  # a separately built SyntheticRecoveryWorker
        policy=SyntheticSupervisorPolicy(
            max_claims_per_pass=5,
            idle_poll_seconds=1.0,
            busy_poll_seconds=0.05,
            error_backoff_seconds=2.0,
            max_consecutive_errors=3,
        ),
    )
    report = service.serve(stop)
    if report.stop_reason == "WORKER_UNHEALTHY":
        raise SystemExit(1)

The hosting process must wire SIGTERM/SIGINT to stop.set(), own secrets and
service identity, limit network egress, and monitor the worker exit status.
This library does not create or supervise an OS-level background daemon.
Use serve(stop, max_passes=...) only for finite local operation/test runs.

Run the isolated demonstration and checks:

    python -m safe_agent m6-worker-demo
    pytest -q tests/m6/test_supervised_worker.py

The demo uses disposable SQLite and repository synthetic fixtures only.

## Failure behavior

- A COMMITTED receipt is counted verified only after the existing trusted
  worker recorded its independent fake-provider evidence in SQL.
- OUTCOME_UNKNOWN is reported as unverified. It may be transient or already
  escalated to NEEDS_REVIEW; only persisted task/outbox evidence distinguishes.
- A crash after a fake effect leaves a durable lease. The restarted worker
  must wait for expiry and reconcile the provider-owned key and readback.
  The supervisor does not re-post an ambiguous effect itself.
- Unexpected ordinary worker exceptions are not treated as known-no-effect.
  Their contents never appear in exported supervisor summaries. Polling
  backs off, and reaching the failure threshold halts the instance.
- InjectedCrash and other BaseException termination paths are not swallowed.
  A halted instance cannot silently restart itself.
- An operator stop signal interrupts the sleep interval; it does not discard
  queued SQL actions.

## Security and operational limitations

SupervisedSyntheticWorker requires the exact SyntheticRecoveryWorker type,
which itself requires exact FakeCalendarAdapter and FakeInternalTaskAdapter
types. Non-synthetic/Google-bound outbox rows are never claimed. There is
no model-facing or browser-facing worker run route. Enabling the supervisor
does **not** enable Google writes.

Counters are intentionally non-durable. TAL's shadow audit journal remains
non-atomic with SAFE's authoritative SQL ledger, and in-memory audit sync
warnings do not constitute durable incident alerting. Production-grade
separate processes, SSO, key rotation, worker supervision, monitoring, backup,
deployment constraints and independent Google-hosted acceptance remain future
release blockers.

## Required tests

The M6 suite checks calendar/internal task auto-processing, bounded batches,
idempotent repeated polling, uncertain response reconciliation, lease recovery
after postcommit crash, Google outbox isolation, interruptible idle wait,
bounded error backoff, fail-closed failure threshold, malformed policy/clock,
and the CLI demonstration. Also run full pytest, mypy, Ruff, Node syntax,
TAL conformance and Alembic smoke.

**M5 LIVE ACCEPTANCE IS STILL BLOCKED.** The synthetic supervisor never
performs real provider effects or grants live authority.
