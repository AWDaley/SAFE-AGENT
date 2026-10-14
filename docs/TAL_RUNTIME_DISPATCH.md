# TALRuntime at SAFE-AGENT's durable synthetic outbox


> **Historical implementation snapshot.** This note records an earlier
> stage and its then-current upstream SHA; some earlier "not implemented" text
> is superseded. As of PR5, the only currently pinned TAL source is
> [`ci/tal-office-commit.sha`](../ci/tal-office-commit.sha), with experimental
> trusted office authorization and mock-only Google execution. **M5 LIVE
> ACCEPTANCE: BLOCKED.** See [M5_RELEASE_GATE.md](M5_RELEASE_GATE.md).

**Status:** Additional, opt-in M5 interoperability check. Real Google provider writes remain disabled.

**Upstream requirement:** TAL commit `c869390752f4a142e0a9605bb211a827510d1cff` (calendar runtime action). This change exercises the **actual `TALRuntime.apply_proposal` office branch** rather than only the preview-oriented `tal.office.admit_office_calendar` function. The runtime requires trusted run/actor identity, a pinned state version, and an `EXECUTING` outbox phase. The sealed `OfficeRuntimeAction` also contains the hash of an already-persisted leased outbox action. TAL independently evaluates calendar bounds, approval binding, budgets, and signed-grant hash integrity. Its decision still declares `authorizes_execution=False`.

`SyntheticRecoveryWorker(..., tal_dispatch=TrustedTALOfficeDispatchCheck())` reconstructs the request from SQL-backed manifest/proposal/simulation/task/approval state and invokes TAL before **a new** synthetic calendar dispatch. TAL absence, version mismatch, malformed evidence, denial, or unexpected execution capability escalates to `NEEDS_REVIEW` with zero new provider effects. The worker independently verifies the broker handles and latest synthetic provider revision, and maintains the idempotency and read-back behavior already tested in M4.

**Recovery exception:** when the synthetic provider already committed an identical action before the worker crashed, the worker reconciles it by durable provider action key and read-back, even if TAL is unavailable or the old approval has expired. This avoids retrying or concealing a real prior effect. It does not authorize a new dispatch.

**Compatibility note:** the existing M0–M4 synthetic demos still work without TAL installed; they must remain synthetic-only. The TAL requirement is opt-in for this tested M4 worker. It is **mandatory before any future external provider write**; a Google calendar adapter cannot be passed to `SyntheticRecoveryWorker` and `GoogleTestCalendarService.execute_model_proposal` remains fail-closed. The upstream TAL office branch does not yet persist its own office event ledger or issue trusted execution receipts. SAFE-AGENT's SQLite/PostgreSQL ledger remains the durable task/outbox authority for this test workflow.

## Verification

- Run base offline tests with `pytest -q`.
- Install pinned TAL from a reviewed commit; run `pytest -q tests/m4/test_tal_runtime_dispatch.py tests/m5/test_upstream_office_bridge.py`.
- Assert a missing/import-failing TAL runtime creates zero new bookings and requires review.
- Assert exactly one booking with the real TAL runtime and no duplicate after crash/recovery.
- Verify that Google insertion cannot be reached by any branch of the synthetic worker.

**Still blocking production:** shared authoritative TAL office ledger/replay integration, signed/version-bound capability enforcement across processes, genuine Google recovery/idempotency tests using a disposable account, external secret management, OS/network isolation, and security review.
