# SAFE-AGENT Google Calendar test connection — operator setup

This file describes **test-only deployment prerequisites**. M5 currently has no public OAuth-callback HTTP endpoint or model-facing real-calendar execution. Do not paste OAuth codes, refresh tokens, client secrets, Fernet keys, or real calendar IDs into ChatGPT, an agent prompt, logs, repository commits or support tickets.

1. Create a *separate, disposable Google account* (or a clearly isolated secondary calendar owned by a test account). Populate it with synthetic events. Never use `primary` or a business calendar. Copy its exact internal calendar ID to trusted server configuration (not model context). Review Google permissions and ownership. The code requires this value in `allowed_test_calendar_ids`; omission refuses initialization.
2. In Google Cloud, enable the Calendar API, configure a test-mode OAuth consent screen/test users, and provision a **web-server OAuth client** with an exact redirect URI. Use only the two approved scopes: `calendar.events.owned` and `calendar.events.freebusy`. These are deliberately less broad than full Calendar access. If a given account/calendar cannot authorize these scopes, stop and investigate rather than expanding them silently.
3. Supply `OAuthClientConfig(client_id, client_secret, redirect_uri)` to the **trusted operator backend**, not the model process. Store a Fernet key in a secret manager with restricted access. On loss of that key, encrypted connections cannot be recovered; plan key rotation and backups. Instantiate `GoogleCredentialVault(engine, fernet_key=<server_secret>, allowed_test_calendar_ids=(<approved_id>,))` after Alembic migration `0003`.
4. A backend authenticated as the authorized operator calls `GoogleOAuthService.start(...)`. Only the operator should receive its URL and random state. The callback handler must establish the **same authenticated operator**, compare the exact state, and invoke `complete(...)` within ten minutes with Google's returned authorization code. Do not publish OAuth state values or authorization codes to a client-side log. The optional authenticated browser callback router is implemented in safe_agent.integrations.google_calendar.http.create_google_test_router and may be installed only in a trusted operator backend. Real-account end-to-end behavior is not yet verified. Reverse proxies must redact or disable callback query-string access logging.
5. The backend stores only the encrypted refresh token and test calendar ID, scopes and opaque connection handle. On provider reads, call `access_token(...)` inside the privileged backend; never forward it to the planner. `GoogleTestCalendarService.observe(...)` additionally checks the matching registered SAFE-AGENT task manifest and actor scope.
6. To disconnect, invoke the trusted `GoogleOAuthService.revoke(...)` and revoke the application's Google grant in the test account when necessary. Revocation always disables local access, even if the remote endpoint cannot be reached.
7. Do **not** invoke low-level `GoogleCalendarHTTP.insert_test_event(...)` against a real account. The M5 business execution integration is intentionally blocked until a certified TAL office-domain adapter and reviewed durable commit/reconciliation path exist.

### References

- [Google Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth)
- [Google OAuth web-server authorization](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Google freeBusy](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query)
- [Google events insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)
- [Google events get](https://developers.google.com/workspace/calendar/api/v3/reference/events/get)
- [Calendar API errors](https://developers.google.com/workspace/calendar/api/guides/errors)

### Why a read-only preview is not a reservation

Google Calendar free/busy provides a view at the time requested. It does not lock the time slot. Another user can create an event between the check and the attempted insert; a provider-generated ETag or a deterministic ID cannot make the free/busy check transactional with a new event insert. The future trusted worker must revalidate and independently read back, but it must still treat conflicts and unknown outcomes conservatively.
