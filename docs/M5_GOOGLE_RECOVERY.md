# PR3 — Offline Google Calendar execution and recovery

**Scope:** contract-tested HTTP simulator ONLY. No external Google requests are
reachable from this worker, and no model or HTTP endpoint may dispatch it.
Stacked on PR2 (#14), which is stacked on PR1 (#13 and TAL PR #5).

## Trusted execution path

1. PR2 stages a Google-bound outbox, immutable TAL authorization, encrypted-vault
   connection reference and evidence ledger record in one SQL transaction.
2. The new claim_google_test_action claims only Google-labelled rows with both
   the protected outbox/connection binding and TAL record, under CAS and
   expiring leases. The original synthetic worker remains forbidden.
3. OfflineGoogleTestRecoveryWorker requires exact httpx.MockTransport for BOTH
   OAuth and Calendar HTTP. It also requires trusted opaque-handle-to-provider
   calendar mapping, supplied by its privileged test deployment.
4. Before a provider call, it replays SAFE's ledger, validates the exact task,
   manifest, proposal, TAL record, connection and bound Google request hash.
5. It ALWAYS queries the deterministic Google event ID first. Existing matching
   independent readback is sufficient to verify a prior effect without a POST.
6. A new POST is possible only on the very first QUEUED lease, with fresh TAL
   require_office_execution_authorization, broker scope, task/approval budget,
   calendar mapping, and provider free/busy checks.
7. Google free/busy is not an atomic reservation. This recheck reduces the
   race window but does not remove it; the test suite is NOT proof of general
   exactly-once or slot ownership on Google's real service.
8. After the POST, a separate matching events.get is mandatory. Only then are
   the SAFE receipt, VERIFIED outbox status and hash-linked ACTION_VERIFIED
   evidence committed together. HTTP 200 alone never means COMPLETED.
9. A lost response, unknown read, or crash may already have committed an effect.
   On every subsequent lease the worker performs readback ONLY, never another
   POST. Bounded uncertainty escalates NEEDS_REVIEW.

## Remaining release blockers

- The original Virtual Office simulation is synthetic. PR4 needs a reviewed
  provider-sourced simulation and isolated live-test acceptance.
- TAL's timestamped record is not a signed bearer permit; process isolation,
  stronger shared authority/evidence semantics and the separate shadow TAL
  journal need an independent review before real writes.
- The private mapping from SAFE opaque calendar handle to the exact provider
  ID must be securely provisioned and verified in a production-style test.
- No credentials or provider ID are model-visible. The service execute_model_proposal
  remains fail closed. Do not weaken the MockTransport constructor fence or add
  a live execution flag; a separate reviewed live-test worker is required.
- Production-grade identity, key management, security audit, conflict handling,
  incident response and the dedicated test-account acceptance gate remain open.

## Validation

Pinned TAL CI runs tests/m5/test_google_recovery.py against MockTransport
and tests crash after lease/insert, response loss, stale authority, wrong
calendar, revoked OAuth, busy slot, 409 conflict, read failure escalation,
replay and one-effect limits.
