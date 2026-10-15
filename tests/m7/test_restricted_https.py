"""M7-02: restricted sidecar-only HTTPS inference egress, no live providers.

Every network test uses a trusted fake wire/DNS; the socket TLS test replaces
only the OS socket/context and confirms pinned IP + SNI, not live CA status.
"""

from __future__ import annotations

import json
import socket
import ssl
from datetime import timedelta

import pytest
from pydantic import SecretStr, ValidationError

from safe_agent.agent.json_planner import PLANNER_INSTRUCTION, ClosedJsonPlanner
from safe_agent.canonical import sha256
from safe_agent.contracts.inference import (
    InferenceRequest,
    InferenceRoutePolicy,
)
from safe_agent.inference import (
    InferenceGateway,
    OfflineMockInferenceTransport,
)
from safe_agent.inference.restricted_https import (
    RestrictedEgressDenied,
    RestrictedHTTPSInferenceTransport,
    TrustedInferenceEgressPolicy,
    WireReply,
    _PinnedHTTPSConnection,
)
from tests.m2.conftest import NOW
from tests.m7.test_inference_gateway import fixture

PUBLIC = "93.184.216.34"


def policy(**overrides):
    return TrustedInferenceEgressPolicy(
        route_id="route_test_only",
        model_revision="mock_v1",
        host="inference.example.com",
        path="/v1/infer",
        **overrides,
    )


def outbound_request():
    ctx, projection, _ = fixture()
    return InferenceRequest(
        request_id="inf_" + "e" * 32,
        route_id="route_test_only", model_revision="mock_v1",
        issued_at=NOW, deadline_at=NOW + timedelta(seconds=10),
        prompt_digest=sha256({
            "instruction": PLANNER_INSTRUCTION,
            "task": ctx, "availability": projection,
        }),
        task=ctx, availability=projection,
    )


def public_dns(host, port, **kwargs):
    assert host == "inference.example.com"
    assert port == 443
    assert kwargs == {"type": socket.SOCK_STREAM}
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))]


class FakeSecrets:
    def __init__(self, token="synthetic_inference_token_v1_abc"):
        self.token = token
        self.loads = 0

    def load_inference_token(self):
        self.loads += 1
        return SecretStr(self.token)


class FakeWire:
    def __init__(self, *, status=200, mime="application/json",
                 encoding=None, replace=None):
        self.calls = []
        self.status = status
        self.mime = mime
        self.encoding = encoding
        self.replace = replace

    def exchange(self, **kwargs):
        self.calls.append(kwargs)
        request = InferenceRequest.model_validate_json(kwargs["body"])
        mock = OfflineMockInferenceTransport().infer(request)
        body = mock.model_dump_json().encode("utf-8")
        if self.replace is not None:
            body = self.replace(body)
        return WireReply(self.status, self.mime, self.encoding, body)


def build(*, secret=None, wire=None, resolver=public_dns, clock=lambda: NOW,
          egress_policy=None):
    secrets = secret if secret is not None else FakeSecrets()
    channel = wire if wire is not None else FakeWire()
    transport = RestrictedHTTPSInferenceTransport(
        egress_policy or policy(), secrets,
        resolver=resolver, wire=channel, clock=clock,
    )
    return transport, secrets, channel


@pytest.mark.parametrize("host", [
    "localhost", "localhost.example.com", "api.local", "model.internal",
    "metadata.google.internal", "169.254.169.254", "127.0.0.1",
    "https://api.openai.com", "api.openai.com:443",
    "api.openai.com@evil.com", "MODEL.EXAMPLE.COM",
    "model.xn--evil.com", "models.example.com.", "model.test",
    "model.example.com\nAuthorization: Bearer secret",
])
def test_destination_must_be_explicit_trusted_public_fqdn(host):
    with pytest.raises(ValidationError):
        TrustedInferenceEgressPolicy(
            route_id="route_test_only", model_revision="mock_v1",
            host=host, path="/v1/infer",
        )


@pytest.mark.parametrize("path", [
    "/v1/../infer", "//models", "/v1//infer", "/v1/%2e%2e/",
    "/v1/infer?redirect=1", "/v1/infer#fragment", "/v1\\infer",
    "/v1/infer\nHost: evil", "/v1/", "http://private.invalid",
])
def test_path_must_be_fixed_and_normalized(path):
    with pytest.raises(ValidationError):
        TrustedInferenceEgressPolicy(
            route_id="route_test_only", model_revision="mock_v1",
            host="inference.example.com", path=path,
        )


def test_no_credentials_urls_proxy_or_tools_in_untrusted_request():
    req = outbound_request()
    for blocked in ("url", "endpoint", "api_key", "authorization",
                    "model_provider", "proxy", "tools", "google_credentials"):
        assert blocked not in req.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        InferenceRequest.model_validate({
            **req.model_dump(mode="json"),
            "endpoint": "https://attacker.invalid/secret",
        })
    with pytest.raises(ValidationError):
        TrustedInferenceEgressPolicy(
            **policy().model_dump(), bearer_token="a" * 32,
        )
    with pytest.raises(ValidationError):
        policy(max_timeout_seconds=600)


def test_public_dns_pins_exact_numeric_ip_before_loading_token():
    connection, secrets, wire = build()
    reply = connection.infer(outbound_request())
    assert reply.request_id == "inf_" + "e" * 32
    assert secrets.loads == 1
    assert len(wire.calls) == 1
    recorded = wire.calls[0]
    assert recorded["host"] == "inference.example.com"
    assert recorded["ip"] == PUBLIC
    assert recorded["address_family"] == socket.AF_INET
    assert recorded["path"] == "/v1/infer"
    assert recorded["timeout_seconds"] == 10
    assert recorded["max_wire_bytes"] == 8192
    assert recorded["bearer_token"] == secrets.token
    assert recorded["body"] == outbound_request().model_dump_json().encode()
    for forbidden in (b"cal_main_private", b"cust_c1", b"oauth",
                      b"provider_id", b"api_key", b"password"):
        assert forbidden not in recorded["body"]


@pytest.mark.parametrize("bad", [
    "127.0.0.1", "10.0.0.2", "172.16.10.2", "192.168.1.25",
    "169.254.169.254", "0.0.0.0", "255.255.255.255",
    "100.64.0.1", "192.0.2.1", "::1", "fc00::1",
    "fe80::1", "::ffff:127.0.0.1",
])
def test_private_reserved_or_metadata_address_denied_before_secret(bad):
    family = socket.AF_INET6 if ":" in bad else socket.AF_INET
    sockaddr = (bad, 443, 0, 0) if family == socket.AF_INET6 else (bad, 443)
    def resolver(*_args, **_kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]
    transport, secrets, wire = build(resolver=resolver)
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(outbound_request())
    assert secrets.loads == 0
    assert wire.calls == []


def test_mixed_public_and_private_answers_fail_closed_not_pick_public():
    def mixed(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    connection, secrets, wire = build(resolver=mixed)
    with pytest.raises(RestrictedEgressDenied):
        connection.infer(outbound_request())
    assert not secrets.loads and not wire.calls


@pytest.mark.parametrize("dns", [
    lambda *_args, **_kwargs: [],
    lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("PRIVATE_DNS_DATA")),
    lambda *_args, **_kwargs: [
        (socket.AF_UNIX, socket.SOCK_STREAM, 0, "", (PUBLIC, 443)),
    ],
])
def test_bad_dns_denied_and_never_leaks_error(dns):
    transport, secrets, wire = build(resolver=dns)
    with pytest.raises(RestrictedEgressDenied) as exc:
        transport.infer(outbound_request())
    assert "PRIVATE_DNS_DATA" not in str(exc.value)
    assert secrets.loads == 0
    assert wire.calls == []


def test_expired_invalid_or_forged_request_rejected_before_credentials():
    transport, secrets, wire = build()
    req = outbound_request()
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(req.model_copy(update={"route_id": "wrong_route"}))
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(req.model_copy(update={"prompt_digest": "sha256:" + "0" * 64}))
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(req.model_copy(update={"deadline_at": NOW - timedelta(seconds=1)}))
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(req.model_copy(update={"model_revision": "untrusted"}))
    assert secrets.loads == 0 and not wire.calls


@pytest.mark.parametrize("token", [
    "short", "SECRET\nInjected: 1", "SECRET\tInjected: 1",
    "nonasciié" * 8, "a" * 513,
])
def test_inference_only_secret_must_be_valid_header_value(token):
    transport, secrets, wire = build(secret=FakeSecrets(token))
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(outbound_request())
    assert secrets.loads == 1 and not wire.calls


@pytest.mark.parametrize("kind", [
    "redirect", "unauthorized", "rate_limit", "server",
    "gzip", "html", "oversized", "wrong_request", "garbage",
])
def test_no_redirects_untrusted_mime_or_invalid_reply_metadata(kind):
    kwargs = {}
    if kind == "redirect":
        kwargs["status"] = 302
    elif kind == "unauthorized":
        kwargs["status"] = 401
    elif kind == "rate_limit":
        kwargs["status"] = 429
    elif kind == "server":
        kwargs["status"] = 500
    elif kind == "gzip":
        kwargs["encoding"] = "gzip"
    elif kind == "html":
        kwargs["mime"] = "text/html"
    elif kind == "oversized":
        kwargs["replace"] = lambda data: data + b"x" * 9000
    elif kind == "wrong_request":
        def mismatched(data):
            obj = json.loads(data)
            obj["request_id"] = "inf_" + "a" * 32
            return json.dumps(obj).encode()
        kwargs["replace"] = mismatched
    else:
        kwargs["replace"] = lambda _data: b'{"secret":true}'
    transport, _, _ = build(wire=FakeWire(**kwargs))
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(outbound_request())


def test_late_reply_is_rejected_even_after_successful_tls_exchange():
    now = [NOW]

    class LateWire(FakeWire):
        def exchange(self, **kwargs):
            result = super().exchange(**kwargs)
            now[0] += timedelta(minutes=2)
            return result

    transport, _, _ = build(clock=lambda: now[0], wire=LateWire())
    with pytest.raises(RestrictedEgressDenied):
        transport.infer(outbound_request())


def test_wire_failure_is_redacted_from_caller():
    class BrokenWire:
        def exchange(self, **kwargs):
            raise RuntimeError("REAL_INFERENCE_KEY_SHOULD_NEVER_LEAK")

    transport, secrets, _ = build(wire=BrokenWire())
    with pytest.raises(RestrictedEgressDenied) as error:
        transport.infer(outbound_request())
    assert secrets.loads == 1
    assert "REAL_INFERENCE_KEY" not in str(error.value)


def test_pinned_https_socket_uses_numeric_address_with_hostname_sni(monkeypatch):
    recorded = {}

    class FakeSocket:
        def settimeout(self, value):
            recorded["timeout"] = value

        def connect(self, address):
            recorded["connected_address"] = address

        def close(self):
            recorded["closed"] = True

    class FakeContext:
        def wrap_socket(self, sock, *, server_hostname):
            recorded["sni"] = server_hostname
            return sock

    monkeypatch.setattr(socket, "socket", lambda *_args, **_kw: FakeSocket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())
    conn = _PinnedHTTPSConnection(
        "inference.example.com", ip=PUBLIC,
        family=socket.AF_INET, timeout=7.0,
    )
    conn.connect()
    assert recorded["connected_address"] == (PUBLIC, 443)
    assert recorded["sni"] == "inference.example.com"
    assert recorded["timeout"] == 7.0
    conn.close()


def test_full_gateway_accepts_only_mocked_https_sidecar_reply():
    endpoint = policy()
    sidecar, _, wire = build(egress_policy=endpoint)
    gateway = InferenceGateway(
        InferenceRoutePolicy(
            route_id=endpoint.route_id, model_revision=endpoint.model_revision,
        ),
        sidecar, clock=lambda: NOW,
    )
    _, _, text = fixture()
    result = gateway.infer(text)
    assert result.proposal.operation == "calendar.create_event"
    assert result.provider_effect_performed is False
    assert result.model_authorized_execution is False
    assert result.reported_usage is not None
    assert result.reported_usage.independently_verified is False
    assert len(wire.calls) == 1
    assert isinstance(ClosedJsonPlanner(gateway.generate).propose(
        fixture()[0], fixture()[1],
    ).model_dump_json(), str)
