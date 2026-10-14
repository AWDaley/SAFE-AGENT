# ADR 0005 — Google test calendar provider boundary

**Status:** Accepted for isolated offline contract tests; live execution blocked (October 14, 2026).

## Decision

Implement one concrete Google Calendar connector using exact provider APIs, server-side PKCE OAuth, encrypted SQL refresh-token storage, per-organization/actor connection scoping, and an explicit test calendar allowlist. Expose a separate `GoogleCalendarObservation` and no raw calendar/event details to the planner. Use private deterministic event IDs and Google private extended properties as reconciliation aids, **not** guarantees of exactly-once remote behavior.

The existing `TrustedOfficeTasks`, `OfficeGate`, `SyntheticExecutionGateway`, `SyntheticRecoveryWorker` and their M4 leased outbox remain synthetic-only. M5 does not create a second production authorization path. The Google low-level insert contract is tested with an HTTP simulator, and `GoogleTestCalendarService.execute_model_proposal()` always fails closed. A reviewed upstream TAL office-domain pack is a hard prerequisite for enabling model-triggered remote mutation.

## Alternatives rejected

- Replacing `FakeCalendarAdapter` with an arbitrary Google connector subclass: would bypass M3's synthetic type fence and misrepresent upstream TAL compliance.
- Giving the agent an OAuth bearer token or Google client: contradicts the information-isolation and deterministic-authority requirements.
- Treating free/busy + create as an atomic reservation: not supported by Google's generic event creation API.
- Automatically retrying an insert after an HTTP timeout/5xx: could double-book before readback stabilizes, even with a deterministic client event ID.
- Unrestricted Google Calendar scopes and `primary` calendar fallback: unnecessary authority for an isolated test account.

## Follow-up gate

Extend upstream TAL with an office calendar pack and canonical conformance fixtures; build a Google-specific durable provider worker that rechecks task/approval/projection against actual provider state, reconciles remote ambiguous outcomes, and emits trusted verified evidence. Gate release on an actual isolated test account and security review. Until then, Google observations are advisory and remote writes disabled.
