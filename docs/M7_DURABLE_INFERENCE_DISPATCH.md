# M7-05a — Durable inference invocation foundation

**Delivered scope: contracts, pure replay, ORM and forward migration only.**
This increment does not wire a durable coordinator into planning or the sidecar.
It does not yet establish at-most-once backend dispatch, restart-persistent rate
limits, automatic recovery, or a recovered-result cache. Those require M7-05b–e.
**M5 LIVE GOOGLE ACCEPTANCE: BLOCKED. Hosted inference remains unapproved.**

## Integrated starting point

PRs #25, #26, #27 and #28 were merged in dependency order at the repository
owner's explicit request. The resulting main commit was
`394977d6ea5a617722d74ae6618e4ed40eedc61b`. PR #29 remains closed and excluded.
Source integration does not replace the still-outstanding independent security
review, protected-main administration or production deployment acceptance.
Issue #30 remains the security-review record, not a completed sign-off.

## What this increment adds

`contracts/inference_dispatch.py` defines frozen, closed, versioned invocation
bindings, sealed lifecycle events and privacy-minimized views. The binding
connects an inference request to a planning attempt, tenant, task, manifest,
route, model revision, prompt/request digests, source versions and deadline.
`bind_planning_attempt` revalidates Python values before JSON serialization can
normalize an invalid boolean to an integer. It checks task/mode and lifetime
compatibility. It does **not** check live SQL authority or admit a request.

`seal_invocation_event` and `replay_invocation` are pure functions. They validate
hash linkage, request/binding identity, event order, timestamps and lifecycle
transitions. They never invoke a backend, settle a charge, release capacity,
change a task or create an office effect. Hash integrity is not source
authentication: a trusted transactional writer and protected evidence store
remain mandatory.

The allowed lifecycle is:

    RESERVED -> DISPATCH_STARTED -> RESPONSE_VALIDATED
                                -> UNKNOWN
             -> FAILED_BEFORE_DISPATCH

UNKNOWN is terminal in v1. It cannot silently rewind into RESERVED, become a
pre-dispatch failure, or justify a resend. A later reconciliation feature needs
an explicit reviewed contract. A failure before dispatch is not billing proof.
Late successful replies cannot become RESPONSE_VALIDATED in pure replay.

## Persistence and migration

`0010_inference_dispatch` extends the unchanged canonical
`0009_inference_cost_accounts`, which still follows `0008_planning_workspace`.
The lineage gate checks the single exact head and canonical parent chain,
rejects PR #29's competing migration, and pins the LF-normalized contents of
0009. No existing historical revision is rewritten.

The two new tables are `safe_inference_invocations` and
`safe_inference_invocation_events`. There is one invocation per
`(organization_id, task_id, attempt_id)`, and request IDs are unique.
Composite foreign keys bind the invocation to the exact task/manifest,
planning attempt/task/version/lease and budget request/tenant/task/route/model/
prompt/deadline. Candidate unique indexes on existing parent tables support
those references without replacing their rows or resetting any cost balance.
A reservation remains mandatory even for a zero-cost invocation record.

Portable row checks constrain valid states, event sequence/version alignment,
claim field pairing, dispatch markers, fixed reason codes and result metadata.
Invocation records contain hashes, not raw prompts, completions, credentials,
free-form questions or a second set of financial counters. The existing M7-03
money ledger remains the sole cost-accounting implementation.

On migrated PostgreSQL, invocation triggers additionally reject identity
mutation, deletion, invalid initial states, lifecycle rewinds, version/clock
regression, dispatch-marker rewrites and unfenced claim changes. Invocation
events have immutable UPDATE/DELETE protection. These checks are safeguards
against invalid SQL writes, not caller authorization or a runtime admission
service. Sequence/row/event coupling must be transactional in M7-05b.

SQLite is a synthetic testing backend. Enable `PRAGMA foreign_keys=ON` when
exercising relational constraints. PostgreSQL-only triggers are **not** installed
by SQLite or `Base.metadata.create_all`; production requires Alembic migration.
Do not infer production concurrency/immutability from SQLite tests.

The migration intentionally refuses destructive downgrade. Review a database
backup and the index/foreign-key lock impact before any existing deployment
upgrade. Rollback is an application-version rollback or reviewed database
restore, not deletion of invocation or billing evidence.

## Verification

From an installed development checkout:

    python scripts/export_m7_dispatch_schemas.py --check
    python scripts/check_m7_budget_lineage.py
    pytest -q tests/m7/test_inference_dispatch_contracts.py
    pytest -q tests/m7/test_inference_dispatch_schema.py
    pytest -q
    ruff check src tests scripts migrations/versions/0010_inference_dispatch.py
    mypy src/safe_agent
    python scripts/check_m5_release.py --mode offline

The PostgreSQL CI job uses a disposable database, applies 0009 then 0010 and
runs both the existing concurrent budget checks and the new mandatory tests:

    SAFE_AGENT_TEST_POSTGRES_DISPATCH=1 pytest -q tests/m7/test_inference_dispatch_postgres.py

The flag requires a real `postgresql+psycopg://` SAFE_AGENT_DATABASE_URL.
Without the flag, the six PostgreSQL tests are explicitly skipped locally.
With it, missing/wrong database configuration is a failure, not a skip.
The tests verify real triggers, simultaneous uniqueness enforcement, stale
state comparisons and preservation of existing uncertain cost holds.

Synthetic schema fixtures insert minimal parent records directly; they are
not runtime admission evidence. A separate compatibility test also creates a
real M6 task/lease and M7-03 reservation before inserting the new binding.

## Next implementation increment — M7-05b

Implement a trusted, transaction-scoped admission coordinator that reuses the
existing planning lease and canonical cost ledger. Allocate one stable request
ID per attempt, atomically commit invocation/reservation/admission evidence,
and fail closed on conflicting identity or unavailable persistence. Do not
hold a database transaction open across inference.

Then implement M7-05c–e: a single protected sidecar dispatch boundary, persistent
throughput and concurrency accounting, dispatch fencing and UNKNOWN handling,
planning-result recovery, and actual multi-process/crash acceptance. Lease
expiry alone must not prove that a worker stopped or release a possible charge.
The current sidecar's in-memory replay/rate behavior and gateway's per-call
request IDs have not changed in M7-05a; no stronger runtime guarantee is claimed.

Never introduce live credentials, a public inference route, a model-selected
endpoint, unrestricted tools, a second budget ledger or a Google write path
as a shortcut to those milestones.
