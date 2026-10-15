"""Private Unix-socket transport for the coordinated OFFLINE mock topology.

Exactly one request: no TCP, DNS, redirects, proxies or automatic retries.
The caller owns both credentials; neither is sent to the synthetic model.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import struct
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import SecretStr

from safe_agent.contracts.base import require_aware
from safe_agent.contracts.inference import InferenceRequest, InferenceTransportReply
from safe_agent.contracts.inference_coordinator import (
    InferenceDispatchClaim,
    TrustedDispatchAuthenticator,
)
from safe_agent.inference_service.service import TrustedWorkloadAuthenticator


class CoordinatedTransportDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("COORDINATED_TRANSPORT_DENIED")


class _DeadlineSocket(socket.socket):
    """Absolute monotonic deadline, including each buffered HTTP header read."""
    deadline: float

    def _remaining(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("transport deadline")
        self.settimeout(remaining)

    def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
        self._remaining()
        return super().recv_into(buffer, nbytes, flags)

    def sendall(self, data: Any, flags: int = 0) -> None:
        self._remaining()
        return super().sendall(data, flags)


class _Connection(http.client.HTTPConnection):
    def __init__(self, host: str, path: Path, deadline: float) -> None:
        super().__init__(host)
        self.path = path
        self.deadline = deadline

    def connect(self) -> None:
        channel = _DeadlineSocket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            channel.deadline = self.deadline
            channel._remaining()
            channel.connect(str(self.path))
            # This accepted topology is Linux-only, same trusted service identity.
            peer = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", peer)
            if uid != os.getuid():
                raise CoordinatedTransportDenied()
            self.sock = channel
        except BaseException:
            channel.close()
            raise


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("nonfinite response")


class CoordinatedUDSInferenceTransport:
    """Trusted bootstrap only; private owner-only socket and two separate keys."""
    def __init__(self, socket_path: Path, *, expected_host: str,
                 workload_token: SecretStr, dispatch_key: SecretStr,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if (not socket_path.is_absolute() or not expected_host.isascii()
                or not 1 <= len(expected_host) <= 253
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in expected_host)):
            raise ValueError("trusted local route required")
        TrustedWorkloadAuthenticator(workload_token)
        if dispatch_key == workload_token:
            raise ValueError("separate dispatch and workload keys required")
        self._signer = TrustedDispatchAuthenticator(dispatch_key)
        self._socket = socket_path
        self._host = expected_host
        self._token = workload_token
        self._clock = clock

    def send(self, request: InferenceRequest, claim: InferenceDispatchClaim,
             dispatch_event_hash: str) -> InferenceTransportReply:
        conn: _Connection | None = None
        try:
            req = InferenceRequest.model_validate(request.model_dump())
            remaining = (req.deadline_at - require_aware(self._clock())).total_seconds()
            if not 0 < remaining <= 120:
                raise CoordinatedTransportDenied()
            deadline = time.monotonic() + remaining
            path = self._socket
            info, parent = path.lstat(), path.parent.lstat()
            if (path.resolve(strict=True) != path or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or parent.st_mode & 0o077):
                raise CoordinatedTransportDenied()
            envelope = self._signer.seal(req, claim, dispatch_event_hash)
            data = envelope.model_dump_json().encode("utf-8")
            if len(data) > 32_768:
                raise CoordinatedTransportDenied()
            conn = _Connection(self._host, path, deadline)
            conn.request("POST", "/v1/dispatch", body=data, headers={
                "Host": self._host, "Authorization": "Bearer " + self._token.get_secret_value(),
                "Content-Type": "application/json", "Connection": "close",
            })
            response = conn.getresponse()
            if (response.status != 200
                    or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json"
                    or response.getheader("Content-Encoding", "identity") != "identity"
                    or response.getheader("Transfer-Encoding") is not None):
                raise CoordinatedTransportDenied()
            lengths = [v for k, v in response.getheaders() if k.lower() == "content-length"]
            if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 32_768:
                raise CoordinatedTransportDenied()
            raw = response.read(int(lengths[0]) + 1)
            if len(raw) != int(lengths[0]) or time.monotonic() >= deadline:
                raise CoordinatedTransportDenied()
            text = raw.decode("utf-8", errors="strict")
            json.loads(text, object_pairs_hook=_unique, parse_constant=_invalid_constant)
            return InferenceTransportReply.model_validate_json(text)
        except Exception:
            raise CoordinatedTransportDenied() from None
        finally:
            if conn is not None:
                conn.close()
