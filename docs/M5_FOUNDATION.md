# M5 foundation — isolated Google Calendar test adapter

Status: **partial M5**, no live integration, no enabled provider writes.

This change adds a privileged-provider module boundary, a read-only Google Calendar `events.get` adapter using a dependency-injected HTTP transport, and an OAuth Authorization Code + PKCE request builder. Event metadata is reduced to `id`, `etag`, `status`, `start`, and `end` before it can be used by trusted verification code. Authorization failures, rate limits, and server errors produce sanitized exceptions; provider response bodies are never included in exception messages.

The new adapter **cannot** be installed into the M3/M4 synthetic gateway, which enforces an exact fake-provider type. The adapter requires an explicit test-mode flag and only addresses a configured test calendar. Its `create_event` and `free_busy` methods fail closed; they do not send requests.

## Remaining M5 release gates

1. Operator creates a dedicated Google test calendar and a Google Cloud OAuth client. Nothing should use personal or production calendars.
2. Implement a trusted OAuth redirect and callback service with durable one-time state/PKCE binding, token exchange, encrypted secret storage, renewal and revocation, tenant binding, and explicit test-account allowlisting. The included request builder alone does **not** constitute working OAuth.
3. Implement a restricted `freeBusy.query` transport, with properly scoped credentials, verified authorized calendar identity, response bounds, and freshness tracking.
4. Add Google Calendar event creation behind the **actual certified upstream TAL office-domain action gate**. Do not bypass `TrustedOfficeTasks` or add another privileged model tool.
5. Revalidate current provider state before each write. Google Calendar free/busy followed by insert is **not an atomic lock**. Plan for conflicts, race conditions and reconciliation.
6. Provide a durable idempotency/reconciliation contract compatible with the M4 outbox. A timeout must never result in blind event duplication. If ambiguity cannot be resolved, escalate to `NEEDS_REVIEW`.
7. Perform separate post-write reads and validate exact expected postconditions, without exposing raw customer data or credentials to the model.
8. Test revoked authorization, 401, 403, 404, 429, timeout, stale state, unsupported calendars, suspicious event text and crash-after-provider-success in a dedicated nonproduction environment. Execute live tests only after explicit test-account authorization.

**Verification:** `PYTHONPATH=src python -m pytest -q`. New tests are in `tests/m5/test_google_calendar.py`.

Do not mark the full M5 milestone complete until the above gates pass on a test account.
