"""Optional browser OAuth connection routes for isolated Google test calendars.

Only the operator backend can install this router; the planner has no access to
it. The callback requires a short-lived, path-scoped HttpOnly browser cookie
bound to the original operator and state as well as the single-use SQL state.
Provider calendar writes remain blocked by the upstream TAL office-domain gate.
"""

import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from safe_agent.contracts.base import StrictModel
from safe_agent.security.principal import AccessDenied, Principal, SignedOperatorTokens

from .errors import GoogleCalendarError, GoogleConnectionDenied
from .oauth import GoogleOAuthService

_COOKIE_HTTPS = "__Secure-safe-agent-google-oauth"
_COOKIE_LOCAL = "safe_agent_google_oauth_local"


class TestCalendarConnect(StrictModel):
    """Operator input only; provider ID never enters the model context."""

    calendar_id: str = Field(min_length=4, max_length=256)


def create_google_test_router(
    oauth: GoogleOAuthService,
    authenticator: SignedOperatorTokens,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    """Browser callback is opt-in; server must scrub OAuth query strings from logs."""
    redirect = urlsplit(oauth.redirect_uri)
    path = redirect.path
    if not path or path == "/":
        raise ValueError("Google OAuth callback must have a dedicated path")
    secure = redirect.scheme == "https"
    cookie_name = _COOKIE_HTTPS if secure else _COOKIE_LOCAL
    bearer = HTTPBearer(auto_error=False)
    router = APIRouter(tags=["google-test-only"])

    def operator(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            result = authenticator.verify(credentials.credentials, now=clock())
            result.require("connection:manage", result.organization_id, clock())
            return result
        except AccessDenied as error:
            raise HTTPException(status_code=401, detail="invalid connection authorization") from error

    @router.post("/v1/integrations/google-test/authorize")
    def authorize(
        command: TestCalendarConnect,
        response: Response,
        principal: Annotated[Principal, Depends(operator)],
    ) -> dict[str, str]:
        try:
            started = oauth.start(
                principal=principal, calendar_id=command.calendar_id, now=clock(),
            )
        except (AccessDenied, GoogleConnectionDenied, GoogleCalendarError) as error:
            raise HTTPException(status_code=403, detail="test connection denied") from error
        # Reissue a short-lived cookie with ONLY connection:manage authority.
        callback_principal = principal.model_copy(update={
            "scopes": ("connection:manage",),
            "authenticated_until": min(
                principal.authenticated_until, clock() + timedelta(minutes=10),
            ),
        })
        cookie_token = authenticator.issue_for_trusted_operator(callback_principal)
        # The state is unpredictable and is checked again against the operator-
        # bound, one-use SQL record by GoogleOAuthService.complete().
        response.set_cookie(
            cookie_name,
            value=started.state + "." + cookie_token,
            max_age=600, path=path, httponly=True, secure=secure, samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"authorization_url": started.authorize_url, "mode": "TEST_ONLY"}

    @router.get(path, response_class=HTMLResponse)
    def callback(
        request: Request,
        state: str = Query(min_length=40, max_length=512),
        code: str = Query(min_length=8, max_length=2048),
    ) -> HTMLResponse:
        cookie = request.cookies.get(cookie_name, "")
        if "." not in cookie:
            raise HTTPException(status_code=400, detail="OAuth session unavailable")
        expected, identity = cookie.split(".", 1)
        if not hmac.compare_digest(expected, state):
            raise HTTPException(status_code=400, detail="OAuth session unavailable")
        try:
            principal = authenticator.verify(identity, now=clock())
            # A callback cookie has only the purpose-specific permission.
            principal.require("connection:manage", principal.organization_id, clock())
            oauth.complete(principal=principal, state=state, code=code, now=clock())
        except (AccessDenied, GoogleConnectionDenied, GoogleCalendarError) as error:
            raise HTTPException(status_code=403, detail="test Google authorization refused") from error
        # Do not put any Google token, identity or calendar ID in this response.
        result = HTMLResponse(
            "<!doctype html><html><body><p>Test calendar connected. "
            "Return to SAFE-AGENT.</p></body></html>"
        )
        result.delete_cookie(cookie_name, path=path, secure=secure,
                             httponly=True, samesite="lax")
        result.headers["Cache-Control"] = "no-store"
        result.headers["Content-Security-Policy"] = "default-src 'none'"
        return result

    @router.post("/v1/integrations/google-test/{connection_id}/disconnect")
    def disconnect(
        connection_id: str,
        principal: Annotated[Principal, Depends(operator)],
    ) -> dict[str, bool]:
        try:
            remote = oauth.revoke(
                principal=principal, connection_id=connection_id, now=clock(),
            )
        except (AccessDenied, GoogleConnectionDenied, GoogleCalendarError) as error:
            raise HTTPException(status_code=404, detail="test connection unavailable") from error
        return {"local_credentials_revoked": True,
                "remote_revocation_confirmed": remote}

    return router
