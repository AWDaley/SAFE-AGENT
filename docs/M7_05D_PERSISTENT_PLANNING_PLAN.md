# M7-05d — Persistent planning integration

Status: implementation started; design and acceptance plan only. No runtime wiring is claimed in this commit.

## Objective
Connect the opt-in M7-05c TrustedInferenceCoordinator to M6 persistent text-only planning without granting inference output execution authority. Retain all offline and live-provider blocks.

## Implementation sequence

1. Inspect M6 planning leases, task snapshots, approval transitions, and M7 coordinator contracts. Add a trusted attempt-scoped adapter that accepts a real authenticated principal, active lease, sanitized prompt and a configured offline transport. Never expose coordinator keys to a model process.
2. Persist only a closed, versioned validated-proposal record keyed to the existing attempt and request identity, including proposal digest, manifest, task/workspace versions, policy version, and source snapshot hash. Ensure unique insert and immutable replay; avoid storing raw prompts, private handles or unvalidated responses.
3. On retry, load the existing attempt and outcome. Revalidate binding and freshness before reuse; stale output must not authorize an action. If the dispatch marker is STARTED or UNKNOWN and no verified result is durable, return an explicit operator-visible uncertain state and do not redispatch.
4. Feed validated proposals through the existing deterministic simulation, TAL gate and proposal-specific human approval. Never treat inference completion as business-task completion. Only the existing trusted synthetic outbox can produce effects after independent authorization.
5. Keep legacy standalone mock planning as an explicit test-only configuration. Coordinated mode must not fall back to raw inference or hosted providers on any error.

## Required tests

- Real M6 lease and stable request identity; concurrent duplicate attempts never produce duplicate backend sends.
- Crash after model response but before durable proposal insertion; restart returns UNKNOWN without a resend.
- Crash after durable insertion; restart reuses only a valid, fresh, hash-bound result.
- Tampered/stale proposal, policy, manifest, source or workspace version fails closed; cross-tenant replay denied.
- Approval denied, expired, changed, or missing: zero synthetic effects. A validated proposal alone creates zero outbox entries.
- Approved synthetic proposal: one verified fake effect, including crash/restart recovery, with no live Google writes.
- Dedicated PostgreSQL concurrency tests and CI gates; offline sidecar and no-network Docker smoke retained.

## Release gates

No live provider, hosted inference, real customer data, or production enablement. M5 live acceptance remains BLOCKED. Do not merge implementation until full CI, PostgreSQL acceptance and human review pass. Issue #30 remains an independent security-review dependency.
