"""Construction and pinning of trusted HTTPX offline fixture clients.

Checks target the pinned HTTPX API, not arbitrary hostile Python. Fixture
handlers are trusted code and can perform I/O themselves: OS isolation remains
mandatory before untrusted code or real credentials may be present.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import httpx


class OfflineGoogleTransportDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("GOOGLE_OFFLINE_TRANSPORT_CHANGED")


def create_offline_google_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    """Trusted fixture bootstrap only; no client/proxy/mount/auth options."""
    return httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False,
        follow_redirects=False,
    )


def _inspect(client: object) -> tuple[httpx.Client, httpx.MockTransport]:
    # Reject subclasses/overridden request routing before inspecting properties.
    if type(client) is not httpx.Client:
        raise OfflineGoogleTransportDenied()
    checked = cast(httpx.Client, client)
    state = vars(checked)
    transport = state.get("_transport")
    if (type(transport) is not httpx.MockTransport
            or state.get("_mounts") != {}
            or checked.trust_env is not False
            or checked.follow_redirects is not False
            or checked.auth is not None
            or checked.event_hooks != {"request": [], "response": []}
            or checked.is_closed
            or not isinstance(state.get("_default_encoding"), str)
            or any(callable(getattr(httpx.Client, name, None)) for name in state)):
        raise OfflineGoogleTransportDenied()
    mock = cast(httpx.MockTransport, transport)
    if "handle_request" in vars(mock) or "close" in vars(mock):
        raise OfflineGoogleTransportDenied()
    return checked, mock


@dataclass(frozen=True)
class OfflineGoogleClientBinding:
    """Pin client, default transport AND handler; reject subsequent substitution.

    Strictly validated base HTTPX clients remain accepted for existing fixtures.
    New fixtures should use create_offline_google_client. No arbitrary network
    transport, mount, hook, client auth, subclass or method override is admitted.
    """
    client: httpx.Client
    transport: httpx.MockTransport
    handler: object

    @classmethod
    def bind(cls, client: object) -> OfflineGoogleClientBinding:
        checked, transport = _inspect(client)
        return cls(checked, transport, transport.handler)

    def require_unchanged(self, candidate: object) -> None:
        if candidate is not self.client:
            raise OfflineGoogleTransportDenied()
        _, transport = _inspect(candidate)
        if transport is not self.transport or transport.handler is not self.handler:
            raise OfflineGoogleTransportDenied()
