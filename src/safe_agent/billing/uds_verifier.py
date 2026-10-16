"""Private Unix-socket client for the isolated billing-verifier service.

One request only: no TCP, DNS, redirects, proxies or retries.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import struct
import time
from pathlib import Path

from pydantic import SecretStr

from safe_agent.billing.verifier import SyntheticBillingSourceRecord
from safe_agent.contracts.billing_verification import VerifiedBillingAttestation


class BillingVerifierTransportDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("BILLING_VERIFIER_TRANSPORT_DENIED")


class _Connection(http.client.HTTPConnection):
    def __init__(self, host: str, path: Path, timeout: float) -> None:
        super().__init__(host, timeout=timeout)
        self.path = path
        self.billing_timeout = timeout

    def connect(self) -> None:
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            channel.settimeout(self.billing_timeout)
            channel.connect(str(self.path))
            peer = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", peer)
            if uid != os.getuid():
                raise BillingVerifierTransportDenied()
            self.sock = channel
        except BaseException:
            channel.close()
            raise


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate verifier response key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid verifier response constant")


class BillingVerifierUDSTransport:
    """Trusted reconciler client; carries only source evidence and verifier token."""

    def __init__(
        self, socket_path: Path, *, expected_host: str,
        workload_token: SecretStr, timeout_seconds: float = 5.0,
    ) -> None:
        if (not socket_path.is_absolute() or not expected_host.isascii()
                or not 1 <= len(expected_host) <= 253
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in expected_host)
                or type(timeout_seconds) is not float
                or not 0.1 <= timeout_seconds <= 30.0):
            raise ValueError("trusted local billing route required")
        if type(workload_token) is not SecretStr:
            raise TypeError("trusted billing workload secret required")
        value = workload_token.get_secret_value()
        if (not 32 <= len(value) <= 512 or not value.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError("invalid billing workload secret format")
        self._socket = socket_path
        self._host = expected_host
        self._token = workload_token
        self._timeout = timeout_seconds

    def verify(self, record: SyntheticBillingSourceRecord) -> VerifiedBillingAttestation:
        conn: _Connection | None = None
        started = time.monotonic()
        try:
            checked = SyntheticBillingSourceRecord.model_validate_json(record.model_dump_json())
            path = self._socket
            info, parent = path.lstat(), path.parent.lstat()
            if (path.resolve(strict=True) != path or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or parent.st_mode & 0o077):
                raise BillingVerifierTransportDenied()
            data = checked.model_dump_json().encode("utf-8")
            if len(data) > 16_384:
                raise BillingVerifierTransportDenied()
            conn = _Connection(self._host, path, self._timeout)
            conn.request("POST", "/v1/verify", body=data, headers={
                "Host": self._host,
                "Authorization": "Bearer " + self._token.get_secret_value(),
                "Content-Type": "application/json",
                "Connection": "close",
            })
            response = conn.getresponse()
            if (response.status != 200
                    or response.getheader("Content-Type", "").split(";", 1)[0]
                       != "application/json"
                    or response.getheader("Content-Encoding", "identity") != "identity"
                    or response.getheader("Transfer-Encoding") is not None):
                raise BillingVerifierTransportDenied()
            lengths = [v for k, v in response.getheaders() if k.lower() == "content-length"]
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16_384:
                raise BillingVerifierTransportDenied()
            raw = response.read(int(lengths[0]) + 1)
            if (len(raw) != int(lengths[0])
                    or time.monotonic() - started > self._timeout):
                raise BillingVerifierTransportDenied()
            text = raw.decode("utf-8", errors="strict")
            json.loads(text, object_pairs_hook=_unique, parse_constant=_reject_constant)
            return VerifiedBillingAttestation.model_validate_json(text)
        except Exception:
            raise BillingVerifierTransportDenied() from None
        finally:
            if conn is not None:
                conn.close()
