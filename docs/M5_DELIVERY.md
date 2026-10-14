# M5 — Google Calendar isolated-test connector (implementation report)

**Status update (October 14, 2026, PR5): M5 LIVE ACCEPTANCE: BLOCKED.**
The original connector report below describes an earlier M5 snapshot. Stacked
PRs #13–#16 add a TAL office execution-authorization contract, a Google-bound
SQL outbox, an **offline-only** recovery worker and provider-originated advisory
simulation. Their mock-provider tests do not verify any actual Google account.
PR5 consolidates the shared pinned TAL revision and explicit release gate.
See [current gate](M5_RELEASE_GATE.md), [read-only acceptance readiness](M5_ACCEPTANCE_READINESS.md)
and the PR3 [mock recovery report](M5_GOOGLE_RECOVERY.md).

**Historical M5 connector status:** code and offline HTTP contract tests implemented.
No real provider write has occurred; live acceptance remains unverified.

**Baseline:** `AWDaley/SAFE-AGENT` M4 main at `584f8259a2f22037ee0041466985f3ff9ff0b881`.

This milestone adds a narrowly scoped Google Calendar implementation without replacing M3/M4's synthetic-only action gate, leased execution worker, or ledger. It does **not** claim upstream TAL v0.3 calendar certification, production security approval, live Google OAuth validation, or an actual booking in a Google account.

## Delivered

- `src/safe_agent/integrations/google_calendar/`:
  - OAuth2 authorization-code flow with PKCE S256, one-use expiring server-side state, explicit redirect URI, offline refresh-token exchange, and restricted scope verification.
  - Server-side encrypted refresh credentials and provider calendar IDs in SQLAlchemy-backed connection tables. The Fernet encryption key and OAuth app secret must be externally managed and never logged.
  - Explicit **non-empty calendar ID allowlist**; no `primary` calendar, unrestricted discovery, stored model OAuth token, or model-visible provider IDs.
  - Google Calendar free/busy REST adapter returning normalized busy intervals only; a task-scoped service supplies sanitized availability to the model-boundary DTO.
  - Fixed Google HTTPS API endpoints, no HTTP redirect following, bounded timeouts, no raw vendor errors in exceptions, no arbitrary URLs or generic network tool.
  - Trusted event insert format (fixed non-PII title, no attendees, disabled reminders, `sendUpdates=none`), deterministic Google-valid event ID, hash-tagged private extended properties, and independent `events.get` readback for reconciliation.
  - Safe local disconnect: encrypted refresh credential revoked locally, and remote token revocation attempted best effort.
- An isolated SQL migration `0003_google_test_connection` for OAuth state and connection ciphertext.
- Stable `safe_agent.m5.google_test.v1` JSON schemas, offline HTTP simulator, provider-error tests, OAuth/state/tenant tests, and a new `m5-demo` command.
- Previously pinned M0–M3 schema exports remain unchanged.

## Security posture

**Read-only integration:** `GoogleTestCalendarService.observe(...)` verifies `task:read`, `connection:read`, task actor, organization, pinned manifest scope, calendar handle, task deadline, and observation time window before fetching availability. Google token and real calendar ID remain server-side. The resulting `GoogleCalendarObservation` is marked advisory and sourced from `google_test_calendar`; it cannot be mistaken for the M1 synthetic fixture projection.

**Trusted low-level write implementation:** `GoogleCalendarHTTP.insert_test_event(...)` is available only to code in the privileged backend. It is used with `httpx.MockTransport` in offline tests to exercise request shape, 409 duplicate handling, unknown write response, and independent read-back. Its presence does **not** authorize a caller or prove booking correctness.

**Model-triggered Google writes remain disabled:** `GoogleTestCalendarService.execute_model_proposal(...)` always rejects with `TAL_OFFICE_DOMAIN_NOT_CERTIFIED`. Neither `SyntheticExecutionGateway` nor `SyntheticRecoveryWorker` accepts a Google adapter. There is no endpoint or CLI dispatching a real Google event, and no runtime flag that weakens this invariant. Only a future reviewed office TAL pack + M4 outbox integration may enable external effects.

## Google Calendar capability and consistency matrix

| Operation | Provider API | Implemented here | Semantics / limits |
| --- | --- | --- | --- |
| OAuth connect | Google OAuth2 token + consent | Local service API + simulated exchange | Actual consent has NOT been exercised; operator UI/callback wiring pending |
| Token refresh/revoke | OAuth2 refresh/revocation endpoints | Yes, simulated and encrypted locally | Remote revoke best effort; local disconnect immediate |
| Availability | `POST /calendar/v3/freeBusy` | Yes, tested via simulated HTTP | Busy intervals only; no lock or reservation |
| Event lookup | `GET /calendars/{id}/events/{eventId}` | Yes, tested via simulated HTTP | 404 after timeout does not prove that no effect occurred |
| Event insert | `POST /calendars/{id}/events` | Trusted low-level request with mock validation | NO live dispatch; IDs reduce retry ambiguity but collisions are not guaranteed to be atomically detected |
| Conditional reservation | No general calendar-wide atomic reserve-and-create operation | Not supported | Recheck still races with other writers; escalate conflicts, never claim atomicity |
| End-to-end task completion | Trusted TAL + outbox + independent verifier | **Connected only through MockTransport (PR3)** | Real-provider write/readback remains blocked; simulated completion does not satisfy M5 exit |

## Required checks before M5 live-test acceptance

1. Provision a dedicated Google account or secondary calendar containing **only synthetic** test data. Record and explicitly allowlist its exact calendar ID; do not use the real `primary` calendar.
2. Configure a Google Cloud project, Calendar API, OAuth consent test users, and an OAuth web client with an exact redirect URI. Grant only `calendar.events.owned` and `calendar.events.freebusy` if the test calendar is owned by the consenting account; review scopes against Google guidance.
3. Supply OAuth client secret, persistent Fernet key, and SQL connection via a trusted secret manager; do not commit `.env`, refresh tokens, provider IDs or HTTP traces.
4. Perform an authorized operator OAuth handshake and verify connection revocation, token refresh, 401/403, 429, 404, API permission errors and consent removal using test data.
5. Implement, review and certify the **upstream TAL office-domain action gate** and wire M4 outbox and independent provider read-back before routing any model-proposed calendar write.
6. In the isolated test calendar, test successful insert/readback, stale availability, concurrent writers, response timeout after possible commit, deterministic-ID 409, and readback unavailability. Record nonduplication and escalation evidence. Do not claim exactly-once behavior from Google Calendar.
7. Require a separate security review of process/network isolation, key management/rotation, tenant scoping, OAuth consent verification, audit visibility, and incident response before any business account use.

## Run the offline tests and demo

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m safe_agent m5-demo
python scripts/export_m5_schemas.py --check
pytest -q tests/m5
pytest -q
```

`m5-demo` and tests use **MockTransport only**; they never access a Google account. Secrets, raw customer data and private calendar IDs are intentionally absent from the safe output.

## Unresolved blockers

- No actual Google OAuth connection or real API call was performed in this environment; no user-supplied OAuth app client or test calendar is available.
- Upstream TAL now exposes a typed experimental office runtime admission and protected execution-authorization record. These are **not** bearer permissions, and Google live writes remain disabled.
- The optional authenticated OAuth browser callback and protected Google outbox are implemented but require reviewed deployment wiring, real disposable-account consent, independent live acceptance, durable cross-store audit alerting and secret-rotation validation.
- Provider's event ID duplicate detection and free/busy are **not** atomic slot locking; ambiguous remote effects must remain `OUTCOME_UNKNOWN`/`NEEDS_REVIEW` until reconciled.

**Exit judgment:** M5 provider interface and offline contract-test work delivered; **live integration exit gate outstanding**. See ADR 0005 and `docs/M5_GOOGLE_TEST_SETUP.md`.
