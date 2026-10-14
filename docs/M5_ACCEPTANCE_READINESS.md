# M5 PR4 — Isolated Google Calendar acceptance readiness

**Decision as of October 14, 2026: BLOCKED — live acceptance NOT performed.**

This stacked PR adds deterministic, provider-sourced advisory simulation evidence,
protected SQL binding, a constrained operator read-only preflight, and negative
tests. It does not enable real Google event insertion. A passing mock run or
real free/busy probe does not complete M5's live acceptance criterion.

## Prerequisites owned by the operator

1. Merge and review upstream TAL PR #5, SAFE-AGENT PRs #13, #14 and #15, then
   this PR in dependency order. Pin a reviewed TAL release.
2. Provision a disposable Google account with a calendar owned by that
   account and containing only synthetic data. Never use the primary calendar
   or a personal/customer/business calendar.
3. Configure a Google Cloud project, Calendar API, restricted OAuth test
   users, exact HTTPS browser redirect and minimum owned-events/freebusy
   scopes. Verify ownership and stop if narrower scopes do not work.
4. Use external secret management for OAuth client secret, signing keys,
   persistent Fernet key and SQL credentials. Do not place credentials,
   provider IDs, OAuth codes or approval artifacts in prompts, commits or logs.
5. Use the existing opt-in authenticated browser callback behind HTTPS,
   with OAuth query strings redacted at every proxy and web server.
6. Configure one task-scoped opaque calendar handle and an exact trusted
   Google calendar allowlist. Do not make provider ID mappings model-visible.

## Read-only operator preflight

Call GoogleTestAcceptancePreflight(service).run(...) from an authenticated
trusted operator backend using a previously connected test account, valid
task-scoped references, allowed time range, and timezone-aware clock.
The OAuth vault/service independently enforce tenant, actor and allowlist.

The sanitized GoogleReadOnlyPreflight response distinguishes READ_ONLY_PASSED
from BLOCKED. It identifies mock versus configured HTTP transport, but it
does not independently prove ownership of a real test account. For every
response, provider_write_verified=false, m5_live_acceptance=BLOCKED and
production_authorized=false. No event is created or API secret returned.
A read-only preflight is never an executable TAL authority or a booking.

## Provider-originated advisory simulation

The pure simulate_provider_proposal function binds a calendar.create_event
proposal to the exact free/busy observation, manifest, source digest,
connection handle, validity window and selected times.

The resulting simulation explicitly reports holds_slot=false and
advisory_only=true. It never reserves a slot or claims atomicity.

Staging persists the simulation hash and redacted JSON in the same SQL
transaction as the PR1 TAL authority, PR2 Google binding and outbox intent.
The trusted binding reader and full SAFE replay revalidate the simulation.
Legacy PR2 staged actions without this evidence fail closed, not auto-upgraded.
The PR3 worker still rechecks Google free/busy before a new mock insert.

## Required manual live acceptance (not performed)

A separately reviewed and operator-consented live-test worker will be needed
to perform the eventual isolated Google event insertion. It does NOT exist in
this PR, and relaxing the MockTransport fence is not permitted.

Once prepared and independently approved:

1. Two separate operators register and approve a synthetic appointment,
   preserving pinned grant, proposal, approval and action digests.
2. Dispatch once in the disposable test calendar, verify ID, exact time,
   private action/request markers, independent events.get readback and SAFE
   durable verified receipt; cross-check TAL audit integrity.
3. Exercise conflict, stale observation, two competing workers, response
   loss after possible commit, 409 ID collision, 401/403/429, expired
   approval, revocation and delayed readback. Never blind-retry an unknown
   insertion; escalate unresolved outcomes.
4. Capture externally reviewed outcomes, effect counts, missing evidence and
   sanitized hashes only, never raw tokens or customer data.
5. Complete a security review of SSO/operator identity, process isolation,
   explicit network egress, secrets/rotation, approval expiry, incident
   response and cross-service audit consistency.

**M5 exit remains BLOCKED until those steps actually run and provide
independently verified live results.** Offline CI does not certify real Google
API behavior, Google-wide exactly-once semantics or production safety.

## Offline validation

- pytest -q tests/m5/test_google_acceptance_preflight.py
- pytest -q tests/m5/test_google_provider_binding.py (pinned TAL required)
- pytest -q tests/m5/test_google_recovery.py (pinned TAL required)
- ruff check src tests scripts
- mypy src/safe_agent
- alembic upgrade head on an isolated test database

No production write path, bypass flag or live Google secrets are introduced.
