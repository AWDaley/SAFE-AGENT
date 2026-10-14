"""Test-account-only Google Calendar integration. Never model-privileged."""

from .client import GoogleCalendarHTTP
from .contracts import GoogleCalendarObservation, GoogleTestEventIntent, stable_event_id
from .oauth import GoogleCredentialVault, GoogleOAuthService, OAuthClientConfig
from .service import GoogleTestCalendarService

__all__ = [
    "GoogleCalendarHTTP", "GoogleCredentialVault", "GoogleOAuthService",
    "OAuthClientConfig", "GoogleTestEventIntent", "GoogleCalendarObservation",
    "GoogleTestCalendarService", "stable_event_id",
]

# Backward-compatible M5 foundation read-only adapter (no effects).
from .legacy import (
    GoogleCalendarConflict,
    GoogleCalendarError,
    GoogleCalendarTestAdapter,
    GoogleCalendarUnavailable,
    HttpResponse,
)

__all__ += [
    "GoogleCalendarConflict", "GoogleCalendarError", "GoogleCalendarTestAdapter",
    "GoogleCalendarUnavailable", "HttpResponse",
]
