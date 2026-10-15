# M7-01 through M7-04 — Integration security review handoff

**Status: review requested, NOT a security sign-off.** This review packet
is a checklist for an independent human security reviewer. CI passing,
self-review, or a code merge alone does not establish production safety.

## Scope and canonical branch

Merge only in dependency order:

1. PR #25: provider-neutral inference contracts.
2. PR #26: restricted inference-only HTTPS transport.
3. PR #27: durable micro-USD budget reservations and billing attestations.
4. PR #28: offline-only Unix-socket inference service and synthetic verifier.

**PR #29 was closed as superseded without merge.** PR #27's
`0009_inference_cost_accounts`, with `down_revision =
0008_planning_workspace`, is the **only** approved candidate migration.
The alternative PR #29 `0009_inference_budget` must not be installed
or cherry-picked. Do not rewrite migration history to combine these;
any additional features require a reviewed forward migration.

PR #28 includes `scripts/check_m7_budget_lineage.py` and an actual
SQLite Alembic upgrade test. The code should always reject a second
`0009` revision, missing/changed expected tables, and a verifier
API no longer compatible with the selected budget contract.

## Independent review checklist

### PR #25: untrusted inference data boundary
- [ ] Verify model only receives canonical, minimized task/projection JSON
      with task/tenant/calendar bindings and opaque handles.
- [ ] Inspect strict versioned DTOs, duplicate/nonfinite rejection,
      model-produced clarification-text scrubbing and scope validation.
- [ ] Confirm there is no model-facing tool, provider write, arbitrary
      user-specified route, or access to protected reference resolution.

### PR #26: inference egress boundary
- [ ] Confirm a trusted host selects exact FQDN, normalized HTTPS path and
      model revision; untrusted requests cannot select endpoints.
- [ ] Review DNS private/mixed/metadata address checks, pinned numerical
      TCP connection, hostname/SNI/certificate verification and redirect
      refusal. Test pre-DNS and slow-trickle deadline handling.
- [ ] Record that Python-level DNS timeout and network controls alone do
      NOT replace OS egress restrictions, hardened process isolation,
      service mTLS authentication or trusted credential custody.
- [ ] Confirm restricted outbound credentials are inference-only and
      never co-resident with Google/OAuth or database execution secrets.

### PR #27: accounting and mutable trusted SQL
- [ ] Audit every conditional SQL account update and all-or-nothing
      transaction semantics. Confirm no successful model dispatch before
      an atomic reservation.
- [ ] Check task manifest binding, duplicate request exclusion, global,
      tenant and task limits, restart behavior, expired attempt recovery,
      and unknown-charge holds (unknown must NEVER mean refunded).
- [ ] Validate the canonical PostgreSQL migration, constraints and
      immutable-event trigger behavior; inspect concurrent writers.
- [ ] Audit HMAC issuer trust, scope, replay, over-reservation charges,
      ledger hash replay and leaked cross-tenant balances.
- [ ] Record that provider-reported usage and signed *synthetic* evidence
      are NOT independently verified real vendor billing. A genuinely
      separate billing verifier/key and invoice reader are still needed.
- [ ] The gateway's ledger is an optional constructor parameter for
      synthetic tests. A future paid-provider service bootstrap MUST
      refuse to start without mandatory durable accounting.

### PR #28: sidecar process boundary
- [ ] Review UDS pre-bind and owner-only socket, secret-file ownership,
      no public TCP listener, disabled proxy-header trust and log privacy.
- [ ] Confirm only the exact deterministic offline mock is executable;
      real network model inference, office tools and provider credentials
      cannot be selected by the workload or operator.
- [ ] Validate workload authentication before body reads; check
      host/header ambiguity, request replay, rate/inflight bounds and
      model-result deadline/scope checks.
- [ ] Document that per-process replay/rate state is lost after restart
      and ASGI thread cancellation is NOT a hard worker deadline.
- [ ] Check synthetic source verifier key separation and restrictive
      schema. Do not label test HMAC source records as vendor invoices.

## Mandatory evidence before any merge

- [ ] Independent, **non-author** security review approved for each PR;
      review comments resolved without bypassing the review process.
- [ ] Administrator has enabled protection for `main`, including
      required status checks and disallowing direct force pushes.
- [ ] After merge of each lower PR, retarget the next PR to `main` and
      rerun required checks on the newly integrated base/head commit.
- [ ] Run real PostgreSQL migration and concurrency acceptance in CI
      (M7 budget accounts, transaction rollback, immutable triggers).
- [ ] Confirm CI's combined PR #27/PR #28 lineage gate and full tests
      pass with M5 live Google acceptance still **BLOCKED**.
- [ ] Preserve a signed review record identifying the threat-model owner,
      accepted residual risks and production release prerequisites.

### Commands used by the CI gate

    python scripts/check_m7_budget_lineage.py
    pytest -q tests/m7/test_budget_lineage.py
    pytest -q tests/m7
    python scripts/smoke_inference_sidecar.py
    python scripts/check_m5_release.py --mode offline
    alembic upgrade head
    python scripts/check_m7_postgres_concurrency.py

The final command requires an independently provisioned disposable
PostgreSQL service; it is exercised in the dedicated GitHub Actions
`m7-postgres-budget-integration` job, not on production credentials.

## Deferred production release acceptance

**M5 LIVE GOOGLE ACCEPTANCE: BLOCKED.** A successful source merge or
offline model demo does not authorize customer data, hosted inference
tokens, real provider writes, OAuth integrations, live invoice
reconciliation, or unrestricted tools.

Dedicated service mTLS ingress, verified OS/process isolation and
egress policy, bounded DNS / hard model timeouts, durable replay limits,
authenticated real model adapters, real provider invoice verification,
credential rotation and incident monitoring must be independently
tested and approved in subsequent milestones.
