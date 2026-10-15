"""M7-04 offline inference-only ASGI sidecar with bounded workload admission.

Only a separately hosted trusted process should construct this app. The sole
backend accepted here is the deterministic OfflineMockInferenceTransport.
No business adapter, broker, SQL, network model transport or credentials enter
this service. TLS/mTLS and OS isolation are responsibilities of a trusted
gateway forwarding to a permission-restricted local Unix-domain socket.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import Field, SecretStr, field_validator
from starlette.concurrency import run_in_threadpool

from safe_agent.contracts.actions import parse_proposal_json
from safe_agent.contracts.base import Identifier, StrictModel, require_aware
from safe_agent.contracts.inference import InferenceRequest, InferenceTransportReply
from safe_agent.contracts.inference_coordinator import (
    CoordinatedInferenceEnvelope,
    TrustedDispatchAuthenticator,
)
from safe_agent.inference.gateway import _check_candidate
from safe_agent.inference.mock import OfflineMockInferenceTransport

_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


class TrustedInferenceServicePolicy(StrictModel):
    """Operator-owned, immutable sidecar config. No URL or provider key fields."""

    schema_version: Literal["safe_agent.inference_service_policy.v1"] = (
        "safe_agent.inference_service_policy.v1"
    )
    route_id: Identifier
    model_revision: Identifier
    expected_host: str = Field(min_length=4, max_length=253)
    max_request_bytes: int = Field(default=16_384, ge=1024, le=32_768)
    max_response_bytes: int = Field(default=8192, ge=512, le=32_768)
    max_inflight: int = Field(default=2, ge=1, le=32)
    max_requests_per_window: int = Field(default=60, ge=1, le=1000)
    rate_window_seconds: int = Field(default=60, ge=1, le=300)
    replay_ttl_seconds: int = Field(default=300, ge=121, le=1800)
    replay_max_entries: int = Field(default=2048, ge=32, le=20_000)

    @field_validator("expected_host")
    @classmethod
    def normalized_host(cls, value: str) -> str:
        if (_HOST_RE.fullmatch(value) is None or value.endswith(".")
                or ".." in value or value.startswith("localhost.")
                or "xn--" in value):
            raise ValueError("an exact normalized trusted Host is required")
        return value


class TrustedWorkloadAuthenticator:
    """Constant-time digest comparison. Token obtained from a mounted host file."""

    def __init__(self, token: SecretStr) -> None:
        if type(token) is not SecretStr:
            raise TypeError("trusted workload secret required")
        value = token.get_secret_value()
        if (not 32 <= len(value) <= 512 or not value.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError("invalid trusted workload secret format")
        self._digest = hashlib.sha256(value.encode("ascii")).digest()

    def check(self, values: list[str]) -> bool:
        if len(values) != 1 or not values[0].startswith("Bearer "):
            return False
        candidate = values[0][7:]
        if (not 32 <= len(candidate) <= 512 or not candidate.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in candidate)):
            return False
        return hmac.compare_digest(
            hashlib.sha256(candidate.encode("ascii")).digest(), self._digest,
        )


class ServiceStatus(StrictModel):
    """Aggregate operational state only, with no customer/task/source handles."""

    schema_version: Literal["safe_agent.inference_service_status.v1"] = (
        "safe_agent.inference_service_status.v1"
    )
    mode: Literal["OFFLINE_MOCK_ONLY"] = "OFFLINE_MOCK_ONLY"
    ready: Literal[True] = True
    active: int = Field(ge=0)
    accepted: int = Field(ge=0)
    refused: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    real_provider_enabled: Literal[False] = False
    business_effect_performed: Literal[False] = False


class _WorkloadBusy(Exception):
    pass


class _DuplicateRequest(Exception):
    pass


class _WorkerFailed(Exception):
    pass


class _ServiceDispatch:
    """One in-memory bounded runtime state; fail closed on capacity exhaustion."""

    def __init__(
        self, policy: TrustedInferenceServicePolicy,
        backend: OfflineMockInferenceTransport, *,
        clock: Callable[[], datetime], monotonic: Callable[[], float],
    ) -> None:
        if type(backend) is not OfflineMockInferenceTransport:
            raise TypeError("M7-04 service accepts ONLY the exact offline mock backend")
        self.policy = policy
        self._backend = backend
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._capacity = threading.BoundedSemaphore(policy.max_inflight)
        self._recent: deque[float] = deque()
        self._seen: dict[str, float] = {}
        self._active = 0
        self._accepted = 0
        self._refused = 0
        self._completed = 0
        self._failed = 0

    def reject(self) -> None:
        with self._lock:
            self._refused += 1

    def status(self) -> ServiceStatus:
        with self._lock:
            return ServiceStatus(
                active=self._active, accepted=self._accepted,
                refused=self._refused, completed=self._completed,
                failed=self._failed,
            )

    def dispatch(self, request: InferenceRequest) -> InferenceTransportReply:
        policy = self.policy
        try:
            req = InferenceRequest.model_validate_json(request.model_dump_json())
            now = require_aware(self._clock())
            if (req.route_id != policy.route_id
                    or req.model_revision != policy.model_revision
                    or req.issued_at > now or req.deadline_at <= now
                    or req.availability.expires_at <= now):
                raise _WorkloadBusy()
        except (ValueError, TypeError):
            self.reject()
            raise _WorkloadBusy() from None

        if not self._capacity.acquire(blocking=False):
            self.reject()
            raise _WorkloadBusy()
        admitted = False
        try:
            with self._lock:
                tick = self._monotonic()
                while self._recent and tick - self._recent[0] >= policy.rate_window_seconds:
                    self._recent.popleft()
                for ident, seen_at in tuple(self._seen.items()):
                    if tick - seen_at >= policy.replay_ttl_seconds:
                        del self._seen[ident]
                if req.request_id in self._seen:
                    self._refused += 1
                    raise _DuplicateRequest()
                if (len(self._recent) >= policy.max_requests_per_window
                        or len(self._seen) >= policy.replay_max_entries):
                    self._refused += 1
                    raise _WorkloadBusy()
                # One-use ID is consumed BEFORE executing the backend.
                # Even a failure or timeout must not permit direct replay.
                self._seen[req.request_id] = tick
                self._recent.append(tick)
                self._active += 1
                self._accepted += 1
                admitted = True

            answer = self._backend.infer(req)
            if type(answer) is not InferenceTransportReply:
                raise ValueError("unexpected inference transport result")
            if (answer.request_id != req.request_id
                    or answer.route_id != req.route_id
                    or answer.model_revision != req.model_revision
                    or answer.prompt_digest != req.prompt_digest
                    or require_aware(self._clock()) >= req.deadline_at):
                raise ValueError("stale or mismatched inference response")
            if not 0 < len(answer.model_dump_json().encode("utf-8")) <= policy.max_response_bytes:
                raise ValueError("inference reply output limit exceeded")
            checked = parse_proposal_json(answer.output_json)
            candidate = _check_candidate(checked, req.task, req.availability)
            answer = answer.model_copy(update={"output_json": candidate.model_dump_json()})
            with self._lock:
                self._completed += 1
            return answer
        except (_WorkloadBusy, _DuplicateRequest):
            raise
        except Exception:
            if admitted:
                with self._lock:
                    self._failed += 1
            raise _WorkerFailed() from None
        finally:
            if admitted:
                with self._lock:
                    self._active -= 1
            self._capacity.release()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate inference envelope key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


def create_inference_service(
    policy: TrustedInferenceServicePolicy,
    authenticator: TrustedWorkloadAuthenticator,
    *,
    backend: OfflineMockInferenceTransport | None = None,
    dispatch_authenticator: TrustedDispatchAuthenticator | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Minimal ASGI-only app. Never mount the operator API into this process."""
    if type(policy) is not TrustedInferenceServicePolicy:
        raise TypeError("trusted inference-service policy required")
    if type(authenticator) is not TrustedWorkloadAuthenticator:
        raise TypeError("trusted exact authenticator required")

    if dispatch_authenticator is not None and type(dispatch_authenticator) is not TrustedDispatchAuthenticator:
        raise TypeError("exact trusted dispatch authenticator required")
    dispatch = _ServiceDispatch(
        policy,
        backend if backend is not None else OfflineMockInferenceTransport(),
        clock=clock, monotonic=monotonic,
    )
    app = FastAPI(
        title="SAFE-AGENT isolated inference-only service",
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    def _authenticate(request: Request) -> bool:
        return (
            request.headers.getlist("host") == [policy.expected_host]
            and authenticator.check(request.headers.getlist("authorization"))
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        # Does not reveal current jobs, software/connector state or credentials.
        return {"status": "ready", "mode": "offline_mock_only"}

    @app.get("/internal/status")
    def status(request: Request) -> Response:
        if not _authenticate(request):
            dispatch.reject()
            return Response(status_code=401, content=b"", headers={"Cache-Control": "no-store"})
        return Response(
            dispatch.status().model_dump_json(),
            media_type="application/json", headers={"Cache-Control": "no-store"},
        )

    # Coordinated mode does not expose the legacy raw inference endpoint.
    @app.post("/v1/dispatch" if dispatch_authenticator is not None else "/v1/infer")
    async def inference(request: Request) -> Response:
        if not _authenticate(request):
            dispatch.reject()
            return Response(status_code=401, content=b"", headers={"Cache-Control": "no-store"})
        if (len(request.headers.getlist("content-type")) != 1
                or request.headers["content-type"].split(";", 1)[0].lower()
                   != "application/json"
                or request.headers.get("content-encoding", "identity") != "identity"):
            dispatch.reject()
            return Response(status_code=415, content=b"", headers={"Cache-Control": "no-store"})
        output = bytearray()
        try:
            async for chunk in request.stream():
                if len(output) + len(chunk) > policy.max_request_bytes:
                    dispatch.reject()
                    return Response(
                        status_code=413, content=b"",
                        headers={"Cache-Control": "no-store"},
                    )
                output.extend(chunk)
            parsed = json.loads(
                output.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_pairs, parse_constant=_reject_constant,
            )
            if not isinstance(parsed, dict):
                raise ValueError("not one inference request object")
            if dispatch_authenticator is not None:
                envelope = CoordinatedInferenceEnvelope.model_validate_json(
                    output.decode("utf-8", errors="strict"),
                )
                admitted = dispatch_authenticator.verify(envelope)
            else:
                admitted = InferenceRequest.model_validate_json(
                    output.decode("utf-8", errors="strict"),
                )
        except Exception:
            dispatch.reject()
            return Response(status_code=400, content=b"", headers={"Cache-Control": "no-store"})
        try:
            answer = await run_in_threadpool(dispatch.dispatch, admitted)
        except _DuplicateRequest:
            return Response(status_code=409, content=b"", headers={"Cache-Control": "no-store"})
        except _WorkloadBusy:
            return Response(status_code=429, content=b"", headers={"Cache-Control": "no-store"})
        except _WorkerFailed:
            return Response(status_code=503, content=b"", headers={"Cache-Control": "no-store"})
        except Exception:
            return Response(status_code=503, content=b"", headers={"Cache-Control": "no-store"})
        return Response(
            answer.model_dump_json(), media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return app
