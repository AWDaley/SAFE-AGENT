"""Regression cases for supported offline worker construction; no TAL or network."""
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from safe_agent.integrations.google_calendar.client import GoogleCalendarHTTP
from safe_agent.integrations.google_calendar.recovery import (
    GoogleDispatchDenied,
    OfflineGoogleTestRecoveryWorker,
)
from safe_agent.integrations.google_calendar.service import GoogleTestCalendarService


class SentinelTransport(httpx.BaseTransport):
    """Records routing without opening a socket."""

    def __init__(self):
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        return httpx.Response(200, json={})


class NoClaimRepository:
    def __init__(self):
        self.claims = 0

    def claim_google_test_action(self, **kwargs):
        self.claims += 1
        return None


def worker_for(calendar_http, oauth_http):
    repo = NoClaimRepository()
    service = GoogleTestCalendarService(
        SimpleNamespace(_http=oauth_http), GoogleCalendarHTTP(calendar_http), repo,
    )
    worker = OfflineGoogleTestRecoveryWorker(
        repo, None, service,
        trusted_calendar_mapping={"ref_calendar": "sandbox@group.calendar.google.com"},
    )
    return worker, service, repo


@pytest.mark.parametrize("target", ["calendar", "oauth"])
def test_mounted_transport_is_denied_before_worker_construction(target):
    sentinel = SentinelTransport()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)),
                      trust_env=False) as clean:
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            mounts={"https://": sentinel}, trust_env=False,
        ) as mounted:
            # A default MockTransport says nothing about the selected route.
            assert type(mounted._transport) is httpx.MockTransport
            mounted.get("https://www.googleapis.com/calendar/v3/freeBusy")
            assert sentinel.calls == 1
            with pytest.raises(TypeError, match="OFFLINE ONLY"):
                worker_for(mounted if target == "calendar" else clean,
                           mounted if target == "oauth" else clean)
            assert sentinel.calls == 1


@pytest.mark.parametrize("target", ["calendar", "oauth"])
def test_mounted_replacement_is_denied_before_claim(target):
    sentinel = SentinelTransport()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)),
                      trust_env=False) as clean:
        worker, service, repo = worker_for(clean, clean)
        with httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            mounts={"https://": sentinel}, trust_env=False,
        ) as mounted:
            endpoint = service._api if target == "calendar" else service._oauth
            endpoint._http = mounted
            with pytest.raises(GoogleDispatchDenied, match="OFFLINE_TRANSPORT_CHANGED"):
                worker.process_one(now=datetime.now(UTC))
        assert sentinel.calls == 0
        assert repo.claims == 0
