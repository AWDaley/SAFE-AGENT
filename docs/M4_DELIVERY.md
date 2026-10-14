# M4 — Durable synthetic execution and crash recovery

**Status:** Implemented and locally verified against **synthetic providers only**. This is **not** production provider integration or upstream TAL calendar-domain conformance.

## Recovery contract

The M2/M3 `TrustedOfficeTasks` → `OfficeGate` → `SyntheticExecutionGateway` remains the sole admission path. M4 does **not** add any alternative permission engine or model-accessible tool. After successful admission and M3's independent source validation, `SyntheticExecutionGateway.enqueue()` records one `ExecutionRow`, one `OutboxRow`, the signed private reference bindings, and one hash-chained `ACTION_INTENT_RECORDED` ledger event in a **single database transaction**. An intent cannot be reissued for the same task/proposal. The outbox carries only opaque hashes/revisions; private customer/provider IDs remain behind the reference broker and the trusted fake provider.

`SyntheticRecoveryWorker` is the only dispatcher for M4 durable mode. It claims commands with a 30-second bounded lease and compare-and-swap claim updates; a second worker cannot claim until that lease expires. A crash does not clear the claimed action. On expiry, recovery first consults the trusted provider's **durable idempotency registry**, then performs **independent read-back** against the original immutable proposal. If the provider effect exists, it commits the verified receipt and `ACTION_VERIFIED` ledger event without creating a second effect. If the provider has no effect, a new attempt is allowed **only** if native idempotency is present and the manifest, approval, references, time bounds, projection revision, and state fingerprint still match. Otherwise, the task escalates to `NEEDS_REVIEW` with an explicit reason.

The fake provider itself is persisted to SQL using `safe_fake_provider_state` and `safe_fake_provider_effects` (in one transaction per effect). A restart **never reseeds** the original fixture over committed state. Conditional versioned writes and unique action-key records prevent duplicates, including when the worker crashes between the provider commit and the application receipt. The registry is **synthetic provider-native**; it is not a guarantee that a calendar vendor supports the same semantics.

## Observability and reconciliation

| Outbox status | What it means | Permitted automatic behavior |
| --- | --- | --- |
| `QUEUED` | Intent durably recorded, no dispatch lease | First worker may claim and validate |
| `LEASED` | Worker owns bounded dispatch lease | No concurrent claim before expiry |
| `OUTCOME_UNKNOWN` | Write response/readback uncertain; `EXECUTING` task remains unverified | Reconcile via provider action-key lookup; bounded checks |
| `VERIFIED` | Readback matches pinned proposal; receipt and event persisted | No further dispatch |
| `NEEDS_REVIEW` | Preconditions fail or attempts exhausted | Human investigation / new authorized task, never silent retry |

M4 preserves M3's `OfficePhase` wire values: uncertain actions remain **`EXECUTING`** with an `OUTCOME_UNKNOWN` execution/outbox record rather than silently becoming `COMPLETED`. This preserves the frozen M2/M3 schema exports. Operator status is available through the existing authenticated API at `GET /v1/tasks/{task_id}/execution`, subject to the task's organization and `task:read` permission. The response contains no private IDs or bindings. The ledger continues to replay task state deterministically; terminal outcomes must also match their outbox status and stored execution receipt.

## Fault injection and verification matrix

| Test | Expected result |
| --- | --- |
| Process dies after outbox claim but before call | Lease expires; safe revalidation and one eventual effect |
| Process dies after provider commit but before receipt | Provider action-key found; readback verifies without second effect |
| Provider response lost after commit | Remains unknown, later reconciles to verified without duplicate |
| Postcondition reader unavailable | Remains unknown, bounded retries, then explicit `NEEDS_REVIEW` |
| Two workers contend for one action | Only one lease holder; stale worker cannot settle another lease |
| Unrelated external change invalidates projection | No new dispatch; `NEEDS_REVIEW` escalation |
| Approval expires before dispatch | No new dispatch, even with a queued previously admitted intent |
| Synthetic rate limit before write | Delayed recovery; no unverified effect is counted as complete |
| Complete and replay across process restart | Same action ID, verified effect, sealed ledger, one provider effect |
| Tampered outbox/proposal hash or provider content | Replay or trusted provider integrity check fails |
| Calendar/task operations with distinct adapters | Same intent, leasing and reconciliation rules |

Run the full synthetic suite and demonstration:

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
pytest -q
python -m safe_agent m4-demo
```

The demo injects a crash **after synthetic provider commit**, restarts the repository, private reference broker and provider facade using a persistent SQLite database, reconciles the previously committed effect and prints the verified terminal state and outbox summary. It prints no raw customer names, emails or private provider identifiers.

For schema deployment, `migrations/versions/0002_recovery_outbox.py` creates the durable recovery/outbox tables. PostgreSQL deployments must use Alembic; tests use SQLite `create_all`. The migration is intentionally irreversible without restoring a reviewed backup. Existing execution receipts and event formats are unchanged.

## Limitations and next gates

- **Synthetic providers only.** The durable fake provider and the SAFE-AGENT ledger are separate SQL transactions in the *same test database*. This simulates the ambiguous response boundary but does not prove real distributed exactly-once execution. M5 must document the chosen provider's idempotency, reconciliation, and conditional-write guarantees before enabling it.
- **Not upstream TAL-certified.** The separate TAL v0.3 source still has an invoice domain pack, not the office `calendar.create_event` or `tasks.create` contract. The local OfficeGate remains provisional. No production action may rely on it as evidence of TAL certification.
- **No automatic rescheduling** after a stale projection or unavailable slot. A new user-authorized planning cycle is required. A safe failed action cannot reuse expired approval or references for a fresh write.
- **Unknown outcomes can stay unknown.** Provider inconsistencies, read-back failure or exhausted recovery attempts require operator investigation. The system does not assert success or failure without evidence.
- **Security controls remain incomplete for production.** The broker HMAC key must be securely stored across restarts; SQL database authorization, worker egress isolation, key rotation, tamper-resistant audit, service authentication and integration permissions must be hardened before external software writes.
- **M3 legacy in-memory fake pathway is retained** for historical tests and runs synchronously; it is not crash-resilient. Only the M4 durable-provider configuration dispatches through the leased worker.
- **Operational scheduling** of the recovery worker is not a hosted service yet. A trusted deployment must invoke `process_one()` periodically with worker identity and a bounded retry policy; never expose that worker entrypoint to the model.

**Exit gate:** Tested crash/response-loss/expiry scenarios recover or escalate without duplicate synthetic bookings, and read-back-backed `COMPLETED` replays after restart. No live provider integration is enabled.
