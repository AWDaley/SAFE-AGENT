"""Isolated synthetic billing verifier with asymmetric attestation signing.

The service has no SQL, broker, operator-token, inference-dispatch or business
provider interfaces. It accepts only a closed source-signed synthetic billing
record and returns a verifier-signed attestation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import Field, SecretStr, field_validator

from safe_agent.billing.verifier import (
    BillingSourceDenied,
    OfflineAsymmetricBillingVerifier,
    SyntheticBillingSourceRecord,
)
from safe_agent.contracts.base import Identifier, StrictModel
from safe_agent.contracts.billing_verification import VerifiedBillingAttestation

_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


class TrustedBillingVerifierPolicy(StrictModel):
    schema_version: Literal["safe_agent.billing_verifier_policy.v1"] = (
        "safe_agent.billing_verifier_policy.v1"
    )
    expected_host: str = Field(min_length=4, max_length=253)
    issuer_id: Identifier
    verifier_key_id: Identifier
    max_request_bytes: int = Field(default=16_384, ge=1024, le=32_768)
    max_evidence_age_seconds: int = Field(default=3600, ge=1, le=86_400)

    @field_validator("expected_host")
    @classmethod
    def normalized_host(cls, value: str) -> str:
        if (_HOST_RE.fullmatch(value) is None or value.endswith(".")
                or ".." in value or value.startswith("localhost.")
                or "xn--" in value):
            raise ValueError("an exact normalized trusted Host is required")
        return value


class TrustedBillingWorkloadAuthenticator:
    """Separate verifier bearer token; never shared with the inference sidecar."""

    def __init__(self, token: SecretStr) -> None:
        if type(token) is not SecretStr:
            raise TypeError("trusted billing workload secret required")
        value = token.get_secret_value()
        if (not 32 <= len(value) <= 512 or not value.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError("invalid billing workload secret format")
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


class BillingVerifierServiceStatus(StrictModel):
    schema_version: Literal["safe_agent.billing_verifier_status.v1"] = (
        "safe_agent.billing_verifier_status.v1"
    )
    mode: Literal["SYNTHETIC_SOURCE_ONLY"] = "SYNTHETIC_SOURCE_ONLY"
    ready: Literal[True] = True
    real_invoice_reader_enabled: Literal[False] = False
    business_effect_authorized: Literal[False] = False


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate billing evidence key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid billing JSON constant")


def create_billing_verifier_service(
    policy: TrustedBillingVerifierPolicy,
    authenticator: TrustedBillingWorkloadAuthenticator,
    *,
    source_key: bytes,
    issuer_private_key: bytes,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    if type(policy) is not TrustedBillingVerifierPolicy:
        raise TypeError("trusted billing verifier policy required")
    if type(authenticator) is not TrustedBillingWorkloadAuthenticator:
        raise TypeError("trusted billing workload authenticator required")
    verifier = OfflineAsymmetricBillingVerifier(
        source_key=source_key,
        issuer_private_key=issuer_private_key,
        issuer_id=policy.issuer_id,
        verifier_key_id=policy.verifier_key_id,
        max_evidence_age_seconds=policy.max_evidence_age_seconds,
    )
    app = FastAPI(
        title="SAFE-AGENT isolated billing verifier",
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    def authenticated(request: Request) -> bool:
        return (
            request.headers.getlist("host") == [policy.expected_host]
            and authenticator.check(request.headers.getlist("authorization"))
        )

    @app.get("/healthz")
    def health() -> dict[str, object]:
        return BillingVerifierServiceStatus().model_dump(mode="json")

    @app.post("/v1/verify")
    async def verify(request: Request) -> Response:
        if not authenticated(request):
            return Response(status_code=401, content=b"", headers={"Cache-Control": "no-store"})
        if (len(request.headers.getlist("content-type")) != 1
                or request.headers["content-type"].split(";", 1)[0].lower()
                   != "application/json"
                or request.headers.get("content-encoding", "identity") != "identity"):
            return Response(status_code=415, content=b"", headers={"Cache-Control": "no-store"})
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > policy.max_request_bytes:
                    return Response(status_code=413, content=b"",
                                    headers={"Cache-Control": "no-store"})
                body.extend(chunk)
            text = body.decode("utf-8", errors="strict")
            parsed = json.loads(
                text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant,
            )
            if not isinstance(parsed, dict):
                raise ValueError("not one billing source record")
            record = SyntheticBillingSourceRecord.model_validate_json(text)
            attestation = verifier.attest(record, now=clock())
            checked = VerifiedBillingAttestation.model_validate_json(
                attestation.model_dump_json(),
            )
        except BillingSourceDenied:
            return Response(status_code=409, content=b"", headers={"Cache-Control": "no-store"})
        except Exception:
            return Response(status_code=400, content=b"", headers={"Cache-Control": "no-store"})
        return Response(
            checked.model_dump_json(),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return app
