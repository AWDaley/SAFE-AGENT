# TAL office journal ↔ SAFE-AGENT outbox (M5 continuation)


> **Historical implementation snapshot.** This note records an earlier
> stage and its then-current upstream SHA; some earlier "not implemented" text
> is superseded. As of PR5, the only currently pinned TAL source is
> [`ci/tal-office-commit.sha`](../ci/tal-office-commit.sha), with experimental
> trusted office authorization and mock-only Google execution. **M5 LIVE
> ACCEPTANCE: BLOCKED.** See [M5_RELEASE_GATE.md](M5_RELEASE_GATE.md).

**Scope:** synthetic calendar execution only. No Google Calendar write path was added.

## Trusted sequence

1. SAFE-AGENT persists a manifest-bound outbox intent and claims a bounded lease.
2. Before a **new** synthetic write, `TrustedTALOfficeDispatchCheck(office_journal=...)` reconstructs the exact outbox, proposal, task, approval and simulation evidence. A pinned upstream `TALRuntime.apply_proposal` checks and **journals** the decision. Failed or tampered admission prevents a new effect.
3. SAFE-AGENT's independent trusted worker rechecks the synthetic provider's source revision and broker scopes; only then can its exact fake adapter create an event.
4. Read-back verification and the committed SAFE execution receipt remain authoritative. The `TrustedTALOfficeAuditMirror` mirrors the verified receipt or unresolved/review outcome into TAL's append-only office journal.
5. If the process crashes after the SAFE receipt but before journal sync, `SyntheticRecoveryWorker.reconcile_tal_audit(...)` catches up from SAFE's hash-checked stored evidence **without issuing any provider calls**. An idle `process_one(task_id=...)` also attempts this repair.

## Cross-database integrity limitation

The TAL SQLite journal is **not atomically committed** with SAFE-AGENT's SQL transaction. Admission is journaled before the write; post-commit sync is eventual. A shadow-journal failure must not undo an already independently verified write, pretend the effect failed, or cause redispatch. Instead `audit_sync_warning(task_id)` returns `TAL_AUDIT_SYNC_REQUIRED` until reconciliation succeeds. Monitor and alert on this condition; operator-facing dashboards and persistent alert storage are future work. The in-memory warning is not a durable alert. On restart, rerun reconciliation across all completed/outcome-unknown tasks.

A missing TAL admission can **never be backfilled** after a provider effect. This protects against post-hoc claims of authorization. A prior completed SAFE-AGENT action without TAL journaling remains verifiable in SAFE's ledger but is explicitly **not certified as TAL-journaled**.

Neither TAL's office admission decision nor this journal supplies executable bearer authority. The model has no tool, credential, or direct calendar access. The existing synthetic worker's exact fake-adapter type checks remain unchanged.

## Tests

Current CI resolves its one reviewed upstream revision from `ci/tal-office-commit.sha` and runs integration tests for success, lost-response/restart recovery, transient rate limiting, tampered journal, unavailable post-commit audit, and refusal to fabricate historical admission. Run:

```bash
pip install -r requirements.lock
pip install -e '.[dev]'
TAL_SHA="$(python scripts/read_tal_pin.py)"
pip install "git+https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git@${TAL_SHA}"
pytest -q tests/m4/test_tal_office_journal.py
```

## Remaining real-provider gates

- No shared atomic authority/outbox/evidence commit across PostgreSQL and TAL's SQLite journal.
- Google Calendar's duplicate-ID behavior is not a native exactly-once transaction or slot reservation. No safe generic retry policy has been certified.
- A live disposable Google test calendar has not been authorized or exercised, and provider writes remain disabled.
- Production process isolation, key management, and independent security review remain outstanding.
