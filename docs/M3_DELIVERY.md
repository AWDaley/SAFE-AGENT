# M3 Delivery — Verified Synthetic Scheduling and Internal Tasks

**Status:** Implemented in the **synthetic offline environment only**. This is **not a production TAL authorization certification** and **no real provider integrations are enabled**.

## What works

1. A trusted principal registers a closed, immutable task/manifest, reads a privacy-filtered projection, and records a conflict-free simulation.
2. `FakePlanner` selects a slot deterministically; an optional `ClosedJsonPlanner` accepts a text-only transport with sanitized context and rejects all output except one typed proposal. Neither planner receives credentials or connector tools.
3. Existing `TrustedOfficeTasks` and `OfficeGate` admit the proposal under manifest constraints, scoped broker references, cycle/effect budgets, and a proposal-specific approval when required. This is still **provisional office-domain policy**, not upstream TAL's `TALRuntime.apply_proposal`.
4. The **only M3 effect pathway**, `SyntheticExecutionGateway`, accepts the exact synthetic `FakeCalendarAdapter` (or the exact synthetic internal-task adapter). A different provider implementation is rejected. An additional `task:execute` principal scope is required.
5. The gateway rechecks the pinned manifest, actor, approval expiry, simulation hash, projection freshness, broker scopes, and current fake source revision and SHA-256 fingerprint, before recording a durable action intent.
6. The trusted fake writer then conditionally creates one booking or internal task. A separate read-only surface fetches the resulting synthetic record. The gateway compares its private target and fields with the pinned proposal, stores a safe execution receipt, and emits an `ACTION_VERIFIED` event **only after read-back succeeds**.
7. Replay validates the hash-linked event stream, reconstructed task state, and persisted execution receipt binding. `COMPLETED` reflects independently verified **synthetic** state, never a model claim.

### Commands

From the repository root with Python 3.12+ and installed dependencies:

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m safe_agent m3-demo
pytest -q
python scripts/export_m3_schemas.py --check
```

`m3-demo` prints a synthetic before/after booking count, conflict-free preview, admissibility result, verified receipt, replayed terminal state, and hash-linked event timeline. It intentionally excludes private customer names, email addresses, and raw provider identifiers.

## M3 contract and lifecycle additions

- Additive `safe_agent.internal_task_manifest.v1`, `safe_agent.internal_task_record.v1`, and `safe_agent.task_proposal.v1` permit **exactly** `tasks.create` under the same `OfficeGate` and `TrustedOfficeTasks` state machine. The original M0/M2 calendar manifest/proposal interfaces remain unchanged.
- Current event and task snapshot contracts recognize v1 historical data and new v2 state/effect events. `legacy_m2.py` exists solely to reproduce pinned M2 schema exports, **not** as an alternate runtime ledger.
- `READY_TO_COMMIT → EXECUTING → COMPLETED` requires `ACTION_INTENT_RECORDED` and `ACTION_VERIFIED`; unverified outcomes transition to `NEEDS_REVIEW` via `ACTION_UNVERIFIED`. A completed task has precisely one verified synthetic effect and cannot dispatch a second.
- `safe_execution_records` persists one action intent and outcome linked to the proposal and manifest. The event and execution-row integrity are cross-checked during replay.
- M3's task-list adapter reuses the **same action gate and execution ledger**. No generic shell, browser, SQL or email action is authorized.

## Security and failure tests

Automated tests exercise the happy path with and without approval, exact-bound approvals, forbidden principal scopes, cross-actor execution, manifest/revision mismatches, stale source and candidate conflicts, invalid/multi-action model output, fake-adapter duplicate write rejection, private reference isolation, hash-chain replay, tampered execution receipt, and lost read-back. When a write may have succeeded but cannot be verified, the task becomes `NEEDS_REVIEW` and automatic retry is forbidden.

## Explicit M4/M5 blockers

- **Not upstream TAL conformant.** The separate TAL repository's v0.3 domain pack is invoice-specific; calendar/task packs, canonical hash compatibility fixtures, and upstream action-gate conformance are not implemented. SAFE-AGENT's fake-effects pathway must never be replaced with a live adapter simply because local admission succeeded.
- **No crash-safe provider recovery yet.** M3 durably records intent first, but a process crash after a fake effect and before verification can leave `EXECUTING`. M4 must implement durable outbox leasing, idempotent dispatch/reconciliation and test interrupted commits; no blind retry is allowed now.
- **Synthetic adapter state is in memory.** The trusted fake provider is lost on process restart, while the SQL audit ledger may survive. M4 must persist or reconcile authoritative fake/provider state before claiming recoverability.
- **No hardened model sandbox.** Logical Python module boundaries prevent accidental misuse in this demo, but are not an OS/process isolation boundary. Real models and production integrations require restricted credentials, egress policy, audit protection, and tenant isolation.
- **No real calendar or CRM connection.** Credentials are absent, and there is no production authorization, network effect, event notification, or user-facing execution API.
- **Not a broad compliance guarantee.** The local policy enforces the rules it encodes; it cannot determine every legal or business-context judgment.

**Release gate:** This proves verified **synthetic end-to-end scheduling** and the reusable trusted office-domain path. M4 and upstream TAL integration remain required before any real effect may be enabled.
