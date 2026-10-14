# ADR 0007 — Google test-calendar outbox binding (PR 2)

**Status:** Offline staging implemented on a stacked PR branch; no Google
provider write path exists and production integration remains prohibited.

## Decision

The existing SAFE-AGENT action outbox is the SINGLE durable execution plane.
Versioned `provider_kind` separates `synthetic` from `google_test`. The
original `SyntheticRecoveryWorker` and `LedgerRepository.claim_action` ONLY
claim `synthetic` actions, using both an SQL select and conditional update
filter. The worker additionally rejects a non-synthetic claim. Future Google
execution requires a separate explicitly reviewed trusted worker, not a
generic adapter injected into M4's fake writer.

A trusted `GoogleTestOutboxStager` requires the initiating operator's
`task:read`, `task:execute` and `connection:read` scopes and a ready,
version-pinned, exact calendar proposal. It makes a fresh Google free/busy READ
through the existing server-side OAuth vault (with an explicit allowed calendar
ID). A full available slot is necessary but not considered an atomic hold.
The service accepts no model-supplied observation.

Under a single SAFE SQL transaction, the stager stores the protected broker
references, original M4 execution intent/outbox, `ACTION_INTENT_RECORDED` ledger
event, versioned TAL office execution-authorization record (PR 1), and a unique
`safe_google_test_outbox_bindings` row. The binding contains tenant/actor/task,
opaque connection/calendar references, observation hashes, and a pinned request
hash. It stores NO raw Google calendar identifiers, OAuth tokens, client secret,
or user data. The persisted encrypted vault remains authoritative. Cross-tenant
or revoked connections and mismatched authorization/observation fail closed.

The resulting queued action can be inspected by authorized operators, but
**cannot be claimed by any existing worker**. No Google insert, readback,
automatic retry, outcome, or completion is enabled.

## Limitations / PR 3 prerequisites

- The local Virtual Office and task readiness remain synthetic. PR 2 also
  checks fresh Google availability but does not convert synthetic simulation
  into a certified provider-sourced proposal. PR 3 must review and bind the
  provider-specific simulation and maintain source-of-truth revalidation.
- TAL's 60-second authorization lifetime is intentionally short; a future
  worker must freshly check the server-side authority and provider state,
  treat expired grants and changed approvals as denial for NEW writes, and
  reconcile possible already-committed effects without inventing renewal.
- The SAFE SQL ledger and TAL SQLite shadow journal are not atomically shared.
  Admission journaling and independent read-back evidence need reviewed
  semantics before real Google effects. PR 2 never calls the Google insert API.
- Google Calendar has no generic atomic free/busy reservation. A stale
  observation must be refused. SQL hashes alone are not execution signatures
  or an independent security perimeter.
- Operator-only integration setup, external key management, sandboxing and
  real disposable Google test account acceptance are still outstanding.
- PR 1 is an OPEN dependency. Merge and pin reviewed upstream TAL code first,
  then PR 1 SAFE-AGENT, then rebase/review this PR 2.

## Test gates

`pytest -q tests/m5/test_google_test_outbox.py` with pinned TAL proves
non-effectful staging, provider-kind routing, exact binding, duplicate and
tenant denial, expiry/revocation, transaction rollback on TAL failure, and
tamper detection. The default CI without TAL retains its existing synthetic
suite and intentionally skips the optional cross-repository tests.

All Google HTTP access in CI uses `httpx.MockTransport`, never real accounts.
