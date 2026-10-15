"""M7-02 restricted HTTPS inference *egress* for a separately hosted service.

The remote endpoint must speak SAFE-AGENT's closed inference reply protocol.
This adapter NEVER runs in an untrusted model container and has no business
software providers, SQL connections, arbitrary request URLs or tool registry.

Security: fixed trusted FQDN/path, ALL DNS answers publicly routable, connect
to the checked numeric IP, TLS SNI + CA hostname verification, no redirects,
no ambient proxy variables, bounded wire response, and isolated inference-only
credential. It does not establish production process isolation by itself.
"""

from __future__ import annotations

import http.client
import json
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import Field, SecretStr, field_validator

from safe_agent.contracts.base import Identifier, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceRequest, InferenceTransportReply

_HOST_RE = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_DENIED_SUFFIXES = (
    ".localhost", ".local", ".internal", ".invalid", ".test",
    ".onion", ".home.arpa",
)


class RestrictedEgressDenied(ValueError):
    """A fixed-category failure: no token, endpoint payload, IP or provider text."""

    def __init__(self) -> None:
        super().__init__("RESTRICTED_INFERENCE_EGRESS_DENIED")


class TrustedInferenceEgressPolicy(StrictModel):
    """Trusted sidecar config ONLY, never an HTTP request or model schema."""

    schema_version: Literal["safe_agent.inference_egress_policy.v1"] = (
        "safe_agent.inference_egress_policy.v1"
    )
    route_id: Identifier
    model_revision: Identifier
    host: str = Field(min_length=4, max_length=253)
    path: str = Field(min_length=2, max_length=128)
    max_wire_request_bytes: int = Field(default=16_384, ge=1024, le=32_768)
    max_wire_response_bytes: int = Field(default=8192, ge=512, le=65_536)
    max_timeout_seconds: int = Field(default=10, ge=1, le=30)

    @field_validator("host")
    @classmethod
    def public_domain_only(cls, value: str) -> str:
        # ASCII FQDN only. No URL, colon, port, wildcard, IP literal,
        # username, Unicode IDN ambiguity, metadata host or search suffix.
        if (value != value.lower() or _HOST_RE.fullmatch(value) is None
                or value.startswith("localhost.")
                or value.endswith(_DENIED_SUFFIXES)
                or value in {"metadata.google.internal", "instance-data.ec2.internal"}
                or any(label.startswith("xn--") for label in value.split("."))):
            raise ValueError("trusted inference destination must be an explicit public FQDN")
        return value

    @field_validator("path")
    @classmethod
    def fixed_path_only(cls, value: str) -> str:
        if (not value.startswith("/") or value.startswith("//")
                or any(char in value for char in ("?", "#", "\\", "%", "\r", "\n"))
                or any(piece in {"", ".", ".."} for piece in value[1:].split("/"))
                or not re.fullmatch(r"/[A-Za-z0-9/_-]+", value)):
            raise ValueError("trusted inference path must be a single normalized path")
        return value


def _closed_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RestrictedEgressDenied()
        result[key] = value
    return result


def _deny_nonfinite(_token: str) -> None:
    raise RestrictedEgressDenied()


class InferenceCredentialSource(Protocol):
    """Implemented ONLY inside a dedicated trusted inference sidecar host."""

    def load_inference_token(self) -> SecretStr:
        """Return an inference-only token, never a Google/office/provider-write key."""
        ...


@dataclass(frozen=True)
class WireReply:
    status: int
    content_type: str
    content_encoding: str | None
    body: bytes


class PinnedWire(Protocol):
    def exchange(
        self, *, host: str, ip: str, address_family: int, path: str,
        body: bytes, bearer_token: str, timeout_seconds: float,
        max_wire_bytes: int,
    ) -> WireReply:
        ...


def _public_ip_answers(
    host: str, resolver: Callable[..., Sequence[tuple]],
) -> tuple[str, int]:
    try:
        results = resolver(host, 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError, TypeError):
        raise RestrictedEgressDenied() from None
    addresses: list[tuple[str, int]] = []
    try:
        for entry in results:
            family, sock_type, _, _, sockaddr = entry
            if family not in (socket.AF_INET, socket.AF_INET6):
                raise RestrictedEgressDenied()
            if sock_type != socket.SOCK_STREAM:
                raise RestrictedEgressDenied()
            value = ipaddress.ip_address(sockaddr[0])
            if (not value.is_global or value.is_multicast
                    or value.is_unspecified
                    or (isinstance(value, ipaddress.IPv6Address)
                        and value.ipv4_mapped is not None
                        and not value.ipv4_mapped.is_global)):
                # Mixed public/private DNS is denied, not reduced to the
                # public answers. No second DNS lookup after selection.
                raise RestrictedEgressDenied()
            addresses.append((value.compressed, family))
    except (ValueError, TypeError, IndexError, KeyError):
        raise RestrictedEgressDenied() from None
    if not addresses or len(addresses) > 32:
        raise RestrictedEgressDenied()
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS verifies the DNS hostname but TCP connects ONLY to the checked IP."""

    def __init__(
        self, host: str, *, ip: str, family: int, timeout: float,
    ) -> None:
        verified_context = ssl.create_default_context()
        super().__init__(
            host, port=443, timeout=timeout, context=verified_context,
        )
        self._verified_context = verified_context
        self._pinned_ip = ip
        self._pinned_family = family

    def connect(self) -> None:
        raw = socket.socket(self._pinned_family, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            endpoint = ((self._pinned_ip, 443)
                        if self._pinned_family == socket.AF_INET
                        else (self._pinned_ip, 443, 0, 0))
            raw.connect(endpoint)
            # Explicit SNI + trusted CA store + hostname verification.
            self.sock = self._verified_context.wrap_socket(
                raw, server_hostname=self.host,
            )
        except BaseException:
            raw.close()
            raise


class StrictPinnedTLSWire:
    """No HTTP redirects, transparent proxy, gzip or reusable connection pool."""

    def exchange(
        self, *, host: str, ip: str, address_family: int, path: str,
        body: bytes, bearer_token: str, timeout_seconds: float,
        max_wire_bytes: int,
    ) -> WireReply:
        deadline = time.monotonic() + timeout_seconds
        connection = _PinnedHTTPSConnection(
            host, ip=ip, family=address_family, timeout=timeout_seconds,
        )
        try:
            connection.request("POST", path, body=body, headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": "Bearer " + bearer_token,
                "Connection": "close",
            })
            remaining = deadline - time.monotonic()
            if remaining <= 0 or connection.sock is None:
                raise RestrictedEgressDenied()
            connection.sock.settimeout(remaining)
            response = connection.getresponse()
            raw_length = response.getheader("Content-Length")
            if raw_length is not None:
                if (not raw_length.isascii() or not raw_length.isdecimal()
                        or int(raw_length) > max_wire_bytes):
                    raise RestrictedEgressDenied()
            result = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or connection.sock is None:
                    raise RestrictedEgressDenied()
                connection.sock.settimeout(remaining)
                block = response.read1(min(4096, max_wire_bytes + 1 - len(result)))
                if not block:
                    break
                result.extend(block)
                if len(result) > max_wire_bytes:
                    raise RestrictedEgressDenied()
            if time.monotonic() >= deadline:
                raise RestrictedEgressDenied()
            return WireReply(
                status=response.status,
                content_type=response.getheader("Content-Type", ""),
                content_encoding=response.getheader("Content-Encoding"),
                body=bytes(result),
            )
        except (OSError, ValueError, http.client.HTTPException, ssl.SSLError):
            raise RestrictedEgressDenied() from None
        finally:
            connection.close()


class RestrictedHTTPSInferenceTransport:
    """Provider-neutral HTTPS sidecar transport with strict trusted egress.

    Run ONLY as an inference-only service with isolated credentials/egress;
    the operator API and model worker must never construct or expose it.
    """

    def __init__(
        self, policy: TrustedInferenceEgressPolicy,
        credentials: InferenceCredentialSource, *,
        resolver: Callable[..., Sequence[tuple]] = socket.getaddrinfo,
        wire: PinnedWire | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if credentials is None or not callable(
            getattr(credentials, "load_inference_token", None)
        ):
            raise TypeError("dedicated inference credential source required")
        self._policy = policy
        self._credentials = credentials
        self._resolver = resolver
        self._wire = wire if wire is not None else StrictPinnedTLSWire()
        self._clock = clock

    def infer(self, request: InferenceRequest) -> InferenceTransportReply:
        policy = self._policy
        # Defensive revalidation defeats model_copy(update=...) bypasses.
        try:
            trusted = InferenceRequest.model_validate_json(request.model_dump_json())
            now = require_aware(self._clock())
            if (trusted.route_id != policy.route_id
                    or trusted.model_revision != policy.model_revision
                    or now < trusted.issued_at
                    or now >= trusted.deadline_at):
                raise RestrictedEgressDenied()
            remaining = (trusted.deadline_at - now).total_seconds()
            if remaining <= 0 or remaining > 120:
                raise RestrictedEgressDenied()
            body = trusted.model_dump_json().encode("utf-8", "strict")
            if not 0 < len(body) <= policy.max_wire_request_bytes:
                raise RestrictedEgressDenied()
            # No credential read and NO connect until scope, host and DNS pass.
            ip, family = _public_ip_answers(policy.host, self._resolver)
            token = self._credentials.load_inference_token()
            if type(token) is not SecretStr:
                raise RestrictedEgressDenied()
            value = token.get_secret_value()
            if (not 16 <= len(value) <= 512
                    or not value.isascii()
                    or any(char.isspace() or ord(char) < 33 for char in value)):
                raise RestrictedEgressDenied()
            reply = self._wire.exchange(
                host=policy.host, ip=ip, address_family=family,
                path=policy.path, body=body, bearer_token=value,
                timeout_seconds=min(remaining, float(policy.max_timeout_seconds)),
                max_wire_bytes=policy.max_wire_response_bytes,
            )
            if (require_aware(self._clock()) >= trusted.deadline_at
                    or type(reply) is not WireReply
                    or reply.status != 200
                    or reply.content_type.split(";", 1)[0].strip().lower()
                       != "application/json"
                    or reply.content_encoding not in (None, "", "identity")
                    or not 0 < len(reply.body) <= policy.max_wire_response_bytes):
                # Includes every 3xx redirect, 429, 5xx or malformed MIME.
                raise RestrictedEgressDenied()
            safe_text = reply.body.decode("utf-8", errors="strict")
            parsed = json.loads(
                safe_text, object_pairs_hook=_closed_json_pairs,
                parse_constant=_deny_nonfinite,
            )
            if not isinstance(parsed, dict):
                raise RestrictedEgressDenied()
            result = InferenceTransportReply.model_validate_json(safe_text)
            # The caller gateway ALSO validates this binding.
            if (result.request_id != trusted.request_id
                    or result.route_id != trusted.route_id
                    or result.model_revision != trusted.model_revision
                    or result.prompt_digest != trusted.prompt_digest):
                raise RestrictedEgressDenied()
            return result
        except RestrictedEgressDenied:
            raise
        except Exception:
            # Do not leak host/DNS, certificate, header or raw provider errors.
            raise RestrictedEgressDenied() from None
