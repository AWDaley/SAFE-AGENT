# M5 continuation — Browser OAuth callback for isolated test calendars

**Status:** Implemented with **offline simulated Google endpoints**. No Google account consent, live test-calendar write, or upstream TAL calendar action-gate certification has been performed. This does not close M5's live integration acceptance gate.

## What this adds

The existing `GoogleOAuthService`, encrypted `GoogleCredentialVault`, exact calendar allowlist, and test-only provider adapter remain authoritative. This change does **not** create another OAuth implementation, token vault or authorization plane.

An optional FastAPI router introduces an operator-facing connect/disconnect flow:

- `POST /v1/integrations/google-test/authorize` — requires an authenticated `connection:manage` operator. The request contains only a configured test-calendar ID. The backend calls `GoogleOAuthService.start()`, stores the PKCE verifier encrypted, issues a short-lived `HttpOnly` browser cookie bound to the operator/state and returns the authorization URL.
- `GET <registered OAuth redirect path>` — validates the matching cookie/state, checks the cookie's short-lived signed operator identity, then calls `GoogleOAuthService.complete()` to atomically consume the same principal-bound state before exchanging the code. This route returns no access tokens, refresh tokens, Google account details or provider calendar IDs. The cookie is cleared after success.
- `POST /v1/integrations/google-test/{connection_id}/disconnect` — requires authenticated `connection:manage` and uses existing local credential revocation, reporting separately whether Google confirmed remote revocation.

Install the opt-in router with `create_google_test_router(oauth_service, signed_operator_tokens, clock=...)` or supply `google_test_oauth` to the local `create_app` factory. It is not registered by default. The model worker cannot call these endpoints or read the encrypted credential store. Session cookies are marked `Secure` for HTTPS and are scoped to the exact redirect path. Insecure HTTP is supported **only** with the previously permitted `127.0.0.1` loopback development redirect.

## Known limitations and required deployment precautions

- This uses Google's ordinary browser GET callback. **Configure the web server and reverse proxy to redact or disable access logs for the callback query string**; the authorization code is an ephemeral secret. Never log the OAuth URL/state/verifier, provider responses, user identity, or tokens.
- A production operator identity provider, hardened browser session, external key vault, OAuth secret rotation, and end-to-end tenant/process isolation are still required. The existing signed local operator token mechanism is suitable for a tightly controlled development server, not a public multi-tenant SaaS.
- OAuth consent must be authorized by the owner using an explicitly isolated test account/calendar. Passing offline tests is not evidence that live OAuth consent or provider permissions work.
- The upstream TAL office-domain gate is still unimplemented. `GoogleTestCalendarService.execute_model_proposal` must keep blocking all model-triggered writes. Provider free/busy is advisory rather than an atomic appointment hold.

## Verification

`pytest -q tests/m5/test_oauth_browser_callback.py` uses the repository's existing `httpx.MockTransport` fixture. Tests cover authenticated start, state/cookie binding, callback replay, cross-calendar denial, local/remote disconnect semantics, and the lack of external event inserts.

See also [M5 delivery](M5_DELIVERY.md) and [Google test setup](M5_GOOGLE_TEST_SETUP.md).
