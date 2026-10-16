"""Bounded offline configuration checks; all sentinels are network-free."""
from datetime import UTC, datetime

import httpx
import pytest
from httpx._utils import URLPattern

from safe_agent.integrations.google_calendar.offline_transport import (
    OfflineGoogleClientBinding,
    OfflineGoogleTransportDenied,
    create_offline_google_client,
)
from safe_agent.integrations.google_calendar.recovery import GoogleDispatchDenied
from tests.m5.test_offline_boundary_regressions import worker_for


def response(request):
    return httpx.Response(200, json={})


@pytest.mark.parametrize("options", [
    {"trust_env": True},
    {"follow_redirects": True},
    {"auth": ("synthetic", "test-only")},
    {"event_hooks": {"request": [lambda request: None]}},
    {"event_hooks": {"response": [lambda response: None]}},
    {"default_encoding": lambda data: "utf-8"},
])
def test_unsupported_client_configuration_rejected(options):
    with httpx.Client(**{"transport": httpx.MockTransport(response),
                         "trust_env": False, **options}) as client:
        with pytest.raises(OfflineGoogleTransportDenied):
            OfflineGoogleClientBinding.bind(client)


@pytest.mark.parametrize("change", [
    "mount", "handler", "transport", "auth", "hook", "redirects", "environment",
    "client_method", "transport_method", "closed",
])
def test_mutation_is_denied_before_claim(change):
    with create_offline_google_client(response) as client:
        worker, _, repo = worker_for(client, client)
        if change == "mount":
            client._mounts[URLPattern("https://")] = httpx.MockTransport(response)
        elif change == "handler":
            client._transport.handler = lambda request: httpx.Response(200)
        elif change == "transport":
            client._transport = httpx.MockTransport(response)
        elif change == "auth":
            client.auth = ("synthetic", "secret")
        elif change == "hook":
            client.event_hooks["request"].append(lambda request: None)
        elif change == "redirects":
            client.follow_redirects = True
        elif change == "environment":
            client._trust_env = True
        elif change == "client_method":
            client.request = lambda *args, **kwargs: httpx.Response(200)
        elif change == "transport_method":
            client._transport.handle_request = response
        else:
            client.close()
        with pytest.raises(GoogleDispatchDenied, match="OFFLINE_TRANSPORT_CHANGED"):
            worker.process_one(now=datetime.now(UTC))
        assert repo.claims == 0


def test_safe_client_replacement_still_requires_a_new_worker():
    with create_offline_google_client(response) as original:
        with create_offline_google_client(response) as replacement:
            worker, service, repo = worker_for(original, original)
            service._oauth._http = replacement
            with pytest.raises(GoogleDispatchDenied):
                worker.process_one(now=datetime.now(UTC))
            assert repo.claims == 0


def test_client_subclass_and_overridden_routing_are_denied():
    class CustomClient(httpx.Client):
        pass
    with CustomClient(transport=httpx.MockTransport(response), trust_env=False) as client:
        with pytest.raises(OfflineGoogleTransportDenied):
            OfflineGoogleClientBinding.bind(client)


def test_factory_ignores_proxy_environment_and_has_no_options(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://never-contact.invalid:1234")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/offline-only-certificate")
    with create_offline_google_client(response) as client:
        binding = OfflineGoogleClientBinding.bind(client)
        binding.require_unchanged(client)
        assert client._mounts == {}
        assert client.get("https://www.googleapis.com/calendar/v3/freeBusy").status_code == 200
    with pytest.raises(TypeError):
        create_offline_google_client(response, proxy="http://never-contact.invalid")
