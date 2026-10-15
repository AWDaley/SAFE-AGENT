# M7-05b — Atomic inference admission and persistent quotas

**Status:** Opt-in, trusted-host, offline admission increment on M7-05a / PR #31.
This is not the M7-05c dispatcher or M7-05d planning integration.
**M5 LIVE GOOGLE ACCEPTANCE: BLOCKED. Hosted inference remains disabled.**

## Delivered behavior

`TrustedInferenceAdmission.admit(principal, lease, prompt)` binds one issued
M6 text-only `PlanningLease` to one durable inference request and to the
canonical M7-03 cost reservation. It receives the same minimized canonical
planner envelope used by the existing gateway, not free-form instructions,
URLs, credentials, executable tools, or a caller-selected request ID.

The service verifies the authenticated initiator's current prepare/propose
scopes, task and manifest identity, task-ledger replay, exact planning attempt,
workspace version and lease, scheduling scope, projection freshness, and the
trusted route's input/output/estimated-price limits. New admissions require a
live active attempt on an ASSIGNED task with no proposal or external effect.

Within one transaction it records all of the following, or none of them:

- The immutable host admission profile and its monotonic last-seen clock.
- The global/tenant/task cost holds in the **existing** budget ledger.
- The invocation binding and its sealed RESERVED event from M7-05a.
- An immutable admission record binding the input/lease/profile digests.

The request ID is generated once, under the task lock. An exact repeated
attempt returns `disposition=EXISTING` with the original identity and deadline,
without a second reservation or admission event. Changed input, lease, route,
model, limits, or pricing fails closed. Historical exact repeats may return
safe metadata after expiry; this never extends authority or grants dispatch.
Only digests and typed lifecycle metadata are persisted; prompts, free/busy
contents, raw model completions, and credentials are not stored here.

## Transaction and concurrency boundary

`InferenceBudgetLedger.reserve_in_transaction` reuses the canonical reserve
implementation in the caller-owned `TaskTransaction`. It cannot commit or
open a nested independent session. The original `reserve` method still rejects
duplicate IDs and is retained for existing offline tests.

The lock order is task row, singleton admission authority, then canonical
budget accounts. PostgreSQL uses row locks and conflict-safe singleton
creation. The singleton serializes coordinated quota checks across tenants,
including first-time concurrent calls. A second host with a changed profile
is denied rather than silently increasing or resetting limits.

SQLite is a file-backed synthetic test environment. The new opt-in
`write_task(..., serialize_sqlite=True)` uses BEGIN IMMEDIATE before reading;
ordinary repository behavior is unchanged. SQLite is not evidence of
production isolation. Legacy task timestamp columns preserve original wall
time when SQLite strips offsets; PostgreSQL validates timezone-aware instants.

The trusted clock is sampled again after lock acquisition and before returning
a newly created receipt. Expiry or clock regression during admission rolls back
the transaction. No network/model operation is executed while locks are held.

## Persistent throughput policy

`InferenceThroughputLimits` applies global, tenant, and task limits to the
coordinated path. Rate-window usage is counted from immutable SQL admission
records. Outstanding capacity counts RESERVED, DISPATCH_STARTED and UNKNOWN
invocations. Expiring a request, closing a caller process, or changing a
financial hold to UNCERTAIN does **not** release outstanding capacity.
Terminal lifecycle handling and proof of worker termination belong to M7-05c;
this increment has no release, eviction, reset, claim, or dispatch method.

Zero-price requests still require the ledger and consume admission quotas.
Every new reservation uses the conservative REMOTE/unverified binding. The
admission API has no synthetic-refund flag. Neither provider-reported usage
nor a valid proposal nor an admission receipt establishes billing truth.

This is intentionally restrictive: without a dispatcher/recovery supervisor,
admitted invocations remain RESERVED and can exhaust the offline profile.
Do not reset/delete evidence to recover capacity; add the reviewed lifecycle
implementation in the following increment.

## Trusted startup example

```python
from safe_agent.contracts.inference_admission import InferenceAdmissionPolicy
from safe_agent.storage.inference_admission import TrustedInferenceAdmission

# All dependencies and policy are supplied by trusted host startup, not HTTP.
policy = InferenceAdmissionPolicy(route=trusted_route, budget=trusted_budget)
admission = TrustedInferenceAdmission(repository, policy=policy)
receipt = admission.admit(authenticated_principal, issued_planning_lease, safe_prompt)
assert receipt.dispatch_authorized is False
```

This object is **not** a `ClosedJsonPlanner.generate` callback. No new HTTP
endpoint, shell/SQL/model tool, hosted-provider configuration, or business
execution route is exposed. A receipt cannot be submitted to the existing
sidecar or any provider adapter as execution authority.

## Schema and migration lineage

Forward-only `0011_inference_admission` follows unchanged
`0010_inference_dispatch`, which follows unchanged canonical
`0009_inference_cost_accounts`. Both predecessors are content-pinned by the
lineage guard. PR #29's alternative migration remains excluded.

The new tables are `safe_inference_admission_authority` and
`safe_inference_admissions`. Composite foreign keys bind the immutable
admission to its invocation and exact host-profile digest. PostgreSQL triggers
forbid admission-history deletion/mutation and host-profile reset; only a
nonregressing last-seen timestamp is mutable. SQLite test corruption checks
are not a substitute for these PostgreSQL triggers.

## Reproducible validation

```bash
pytest tests/m7/test_inference_admission.py tests/m7/test_inference_admission_schema.py
python scripts/export_m7_admission_schemas.py --check
python scripts/check_m7_budget_lineage.py
pytest
ruff check src tests scripts migrations/versions/0011_inference_admission.py
mypy src/safe_agent
python scripts/check_m5_release.py --mode offline
```

The dedicated PostgreSQL CI job sets
`SAFE_AGENT_TEST_POSTGRES_ADMISSION=1` and runs
`tests/m7/test_inference_admission_postgres.py` against a disposable database.
With that flag present, an unavailable/incorrect PostgreSQL configuration is
a failure, not a skip. Each test migrates its own randomly named schema;
it never deletes or resets other test or deployment data.

Acceptance covers exact-repeat identity, real M6-issued planning leases,
concurrent callers, global/tenant/task admission limits, first-profile races,
full rollback after partial budget updates and late writes, restart-preserved
holds/quotas, changed-profile refusal, stale/fabricated leases, prompt and
history tampering, immutable PostgreSQL evidence, and zero-cost quota controls.

## Remaining work and explicit limitations

This is **atomic admission**, not at-most-once model dispatch. The existing
legacy gateway and standalone offline sidecar are not yet forced through this
coordinator. Their process-local replay/rate controls have not changed.
Do not connect paid inference or claim global enforcement across unmanaged
legacy callers. M7-05c must establish the non-bypassable dispatch boundary,
claim fencing and uncertainty recovery. M7-05d must provide per-attempt planner
adapters and sanitized-result recovery; M7-05e must prove the complete process
crash matrix end to end.

No hard process timeout, production service identity, OS egress proof,
independent vendor-billing verification, live Google acceptance, independent
security sign-off or production approval is delivered by this increment.
Database administrators can still defeat SQL application controls; reviewed
roles, backups, incident handling and deployment isolation remain necessary.
