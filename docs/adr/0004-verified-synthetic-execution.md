# ADR 0004 — M3 synthetic commit, verification, and additive office task domain

**Status:** Accepted for synthetic-only tests (October 13, 2026)

## Decision

M3 retains the M2 trusted task coordinator and its single office-domain gate. A separate `SyntheticExecutionGateway`, constructed only with exact fake adapter types, revalidates every eligible proposal and required approval under the same pinned manifest, resolves private handles through the trusted broker, compares current synthetic source revision and content, and records a durable intent **before** allowing a fake effect. It independently reads the resulting business record and marks `COMPLETED` only when the new source matches the approved proposal. Hash-linked events preserve both verification and uncertainty.

Additive `InternalTaskManifest` and `InternalTaskCreateProposal` types support `tasks.create` through that same gate and state machine, rather than adding an ungoverned second authority plane. Frozen M0/M2 calendar contracts and old event hashes continue to deserialize and replay. New execution event and snapshot states are represented as v2 schema values; pinned M2 export fixtures remain available separately.

The text-only model planner is restricted to sanitized `SafeTaskContext` and M0 `Projection`. It returns one validated proposal and never receives the gateway, broker, credentials, or a tool registry. The fake deterministic planner remains the default.

## Failure semantics

A stale or denied precondition produces **zero fake effects**. The gateway records an intent before dispatch, so a crash before receiving the result leaves an explicitly incomplete task rather than permitting an untracked retry. A failed postcondition read results in `OUTCOME_UNKNOWN` and `NEEDS_REVIEW`, not `COMPLETED`. A repeated call against `COMPLETED`, `EXECUTING`, or `NEEDS_REVIEW` cannot issue another write. M4 owns durable dispatch leasing and reconciliation of interrupted intents.

## Non-goals / limits

This is **not** TAL upstream calendar authorization; only synthetic fake state may be modified. M3 does not provide provider-native idempotency, durable synthetic provider storage, crash-safe commit orchestration, production identity isolation, general software manipulation, or a user-facing execute endpoint. No code path may substitute a real adapter without a separate explicit design review and upstream TAL compatibility gate.
