# M2 delivery: provisional office admission and durable local evidence

## Implemented

- Immutable trusted task-to-manifest hash pinning.
- Model-proposal admission under explicit calendar scope, time window, duration, deadline, cycle and effect budgets.
- Optional exact-proposal approval identity and expiry checks.
- SQLite task/manifest registration and hash-linked, strictly sequenced audit events.
- Replay reconstructs task admission state without a model/provider.
- Optimistic expected-sequence verification prevents stale writers.
- All decisions are **non-effectful**; no provider dispatch or task completion.

## Security and integration limits

This M2 increment is a **provisional local admission layer**, not an upstream TAL v0.3 office-domain integration. The upstream invoice-only TAL pack cannot yet authorize `calendar.create_event`. `AUTHORIZED` here means **admitted for a future trusted dispatcher**, not a permission to commit or an indication that an effect happened. `safe_agent.tal.boundary.UnconfiguredTALAuthority` still denies execution. The SQLite database is a local development ledger and does not yet replace the planned PostgreSQL/Alembic persistence, transaction outbox, or provider-state verification. Until these prerequisites are implemented and tested, all real writes remain disabled.

The ledger detects modification of persisted events but is not a cryptographic authenticity guarantee against an attacker who can rewrite both the database and every hash. Production deployments require storage permissions and an external integrity checkpoint. The admission service currently accepts only trusted typed inputs; production API authentication, durable principal identity checks, approval issuance service and scope-checked broker reconciliation are follow-up requirements.

## Verification

Run `PYTHONPATH=src pytest -q`. New authority tests exercise denied scopes, duration, deadline, approval tampering/expiration, stale sequencing, persistence across restarts, and ledger alteration.

## Further work before claiming full M2

- Implement TAL calendar domain pack in the upstream TAL repository; pin the revision and run conformance fixtures.
- Replace the local admission authority with the genuine TAL gate; keep a **single** authoritative gateway.
- Add PostgreSQL schema/migrations and transactional event append with verified concurrency controls.
- Add trusted authenticated operator API, approval issuance/revocation, cross-tenant tests and redacted structured logging.
- Connect one approved simulation to the admission service, while preserving the prohibition on provider writes.
