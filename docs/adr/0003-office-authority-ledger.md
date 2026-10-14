# ADR 0003 — M2 offline office policy, durable approvals, and append-only evidence

**Status:** Accepted as a synthetic-only milestone (October 13, 2026)
**Starting point:** SAFE-AGENT `main` at `b065f5f846435d865bb985bf6ecb05cdc370e455` (M1).

## Decision

1. **Model and trusted service stay separate.** `TaskRunner` and `FakePlanner` have no reference to the broker, SQL transactions, approvals, provider adapter or credentials. `TrustedOfficeTasks` alone registers manifests, verifies M1-issued previews, accepts a single typed proposal, and records an offline decision. `submit_planning_result` bridges the existing planner without granting it execution authority.
2. **Not upstream TAL conformance.** TAL v0.3 currently implements invoice operations only. `OfficeGate` is an explicitly **provisional** calendar-domain eligibility checker that follows typed, single-gate and state-version invariants but does **not** call upstream `TALRuntime.apply_proposal`. `UnconfiguredTALAuthority` continues to deny real provider operations. In M2 `AUTHORIZED` and `READY_TO_COMMIT` mean *eligible in a synthetic policy check*, not permission to invoke a provider. An actual TAL calendar domain pack must be created and conformance-tested before M3 execution is enabled.
3. **Pinned immutable authority.** A trusted principal creates a task and a manifest with matching tenant, initiator, task ID and SHA-256 digest. The manifest is stored by digest, immutable in the repository API and protected by a PostgreSQL UPDATE/DELETE rejecting trigger. A model cannot create its own grant.
4. **Scoped identity.** The local-only operator API verifies HMAC bearer tokens with a server-only, nondefault signing key, audience and expiry. There is no anonymous grant creation, and task-read/audit/approve endpoints require tenant-scoped roles. Self-approval is forbidden. This is a **local development identity mechanism**, not production SSO/OIDC, key rotation or revocation.
5. **Atomic task transitions.** The ledger, task projection, accepted proposals and approvals share one SQLAlchemy transaction under a PostgreSQL task-row lock. Audit-only denials increment `ledger_sequence` but do not advance `state_version`; accepted operations change both. SQLite tests use a single local writer, not production concurrency semantics.
6. **Append-only evidence.** Every event binds an event kind, tenant/task/principal, explicit time, payload digest, predecessor hash, and resulting projection version. Replay recomputes the entire chain without any model/provider calls and compares the result with the durable projection. The PostgreSQL migration forbids ordinary UPDATE/DELETE on ledger, manifest and approval tables. A DBA/superuser can still tamper with the database; cryptographic external checkpoint anchoring and production backup protection are separate deployment requirements.
7. **Minimum protected persistence.** References are exported as HMAC-integrity-protected private mappings and stored only in a protected table, never in model context, audit events or HTTP responses. **HMAC does not encrypt the mapping**; production requires database encryption, restricted roles and a managed signing-key system. Projections/simulations are sanitized and durable; the source-to-projection cache and broker runtime must be recovered for any later live action.
8. **No effect executor.** The schema reserves a future execution-record table, but the M2 service never writes an execution record, creates an appointment, completes a task, or accesses a real provider. M3 must add provider preconditions, authoritative state reconciliation, independent read-back and a genuine TAL-enforced dispatch boundary.

## Security and limitations

- `approval_id`, the exact proposal hash, tenant, manifest hash and expiry must all match; different or expired approvals cannot authorize a new action. `revalidate_offline_readiness` rechecks even if the snapshot previously reached `READY_TO_COMMIT`.
- The M1 projection is advisory and expires after ten minutes; approval is not a calendar slot reservation. Recheck source revision and current availability immediately before any future provider write.
- A signed token is not sufficient proof of a real-world user's identity if the signing key is poorly protected. Never use the local token minting helper in a public API.
- Running a planner and trusted engine in a single Python process is a **development convenience, not a security sandbox**. Real model workers must run in network- and credential-restricted services.
- PostgreSQL row locks and migration triggers are tested in GitHub CI against ephemeral PostgreSQL, not a live deployment. CI's database credentials are synthetic and not to be reused.
- The baseline `requirements.lock` contains direct version pins only. A transitive hashes-locked deployment artifact and dependency scanning remain release work.

## M2 release criteria

All M0/M1 tests pass; invalid or denied proposals create zero business effects; changed and expired approvals fail; replay detects altered ledger events; correct local synthetic cases reach `READY_TO_COMMIT` without executing; PostgreSQL migration and immutable-table triggers pass CI; no provider connection is present.
