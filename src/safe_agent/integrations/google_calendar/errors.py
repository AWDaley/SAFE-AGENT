"""Sanitized provider failures. Never leak response bodies or token-bearing URLs."""

from __future__ import annotations


class GoogleCalendarError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GoogleConnectionDenied(GoogleCalendarError):
    pass


class GoogleRateLimited(GoogleCalendarError):
    pass


class GoogleUnknownOutcome(GoogleCalendarError):
    """Provider may have committed an effect; reconcile, never blindly retry."""


class GoogleStateConflict(GoogleCalendarError):
    pass


class GoogleTestWriteBlocked(GoogleCalendarError):
    pass
