"""Isolated billing verifier and aggregate provider-cost audit signer.

The service has no SQL, broker, operator-token, inference-dispatch or business
provider interfaces. Request-level reconciliation remains synthetic-only; optional
provider adapters can sign aggregate audit evidence that cannot settle a request.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import Field, SecretStr, field_validator

from safe_agent.billing.adapters import BillingEvidenceAdapter
from safe_agent.billing.audit_adapters import ProviderCostAuditAdapter
from safe_agent.billing.verifier import BillingSourceDenied, OfflineAsymmetricBillingVerifier
from safe_agent.contracts.base import Identifier, StrictModel
from safe_agent.contracts.billing_verification import VerifiedBillingAttestation
from safe_agent.contracts.provider_billing_audit import VerifiedProviderCostAudit

_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


class TrustedBillingVerifierPolicy(StrictModel):
    schema_version: Literal["safe_agent.billing_verifier_policy.v1"] = (
        "safe_agent.billing_verifier_policy.v1"
    )
    expected_host: str = Field(min_length=4, max_length=253)
    issuer_id: Identifier
    verifier_key_id: Identifier
    max_request_bytes: int = Field(default=16_384, ge=1024, le=32_768)
    max_audit_request_bytes: int = Field(default=1_048_576, ge=16_384, le=4_194_304)
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
    provider_cost_audit_enabled: bool = False
    request_level_provider_reconciliation_enabled: Literal[False] = False
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
    adapter: BillingEvidenceAdapter,
    audit_adapter: ProviderCostAuditAdapter | None = None,
    issuer_private_key: bytes,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    if type(policy) is not TrustedBillingVerifierPolicy:
        raise TypeError("trusted billing verifier policy required")
    if type(authenticator) is not TrustedBillingWorkloadAuthenticator:
        raise TypeError("trusted billing workload authenticator required")
    if not isinstance(adapter, BillingEvidenceAdapter):
        raise TypeError("trusted billing evidence adapter required")
    if audit_adapter is not None and not isinstance(audit_adapter, ProviderCostAuditAdapter):
        raise TypeError("trusted provider cost audit adapter required")
    verifier = OfflineAsymmetricBillingVerifier(
        issuer_private_key=issuer_private_key,
        issuer_id=policy.issuer_id,
        verifier_key_id=policy.verifier_key_id,
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
        return BillingVerifierServiceStatus(
            provider_cost_audit_enabled=audit_adapter is not None,
        ).model_dump(mode="json")

    async def read_json_body(request: Request, maximum: int) -> str:
        if (len(request.headers.getlist("content-type")) != 1
                or request.headers["content-type"].split(";", 1)[0].lower()
                   != "application/json"
                or request.headers.get("content-encoding", "identity") != "identity"):
            raise TypeError("billing media type denied")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                raise OverflowError("billing request too large")
            body.extend(chunk)
        text = body.decode("utf-8", errors="strict")
        parsed = json.loads(
            text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant,
        )
        if not isinstance(parsed, dict):
            raise ValueError("one billing object required")
        return text

    @app.post("/v1/verify")
    async def verify(request: Request) -> Response:
        if not authenticated(request):
            return Response(status_code=401, content=b"", headers={"Cache-Control": "no-store"})
        try:
            text = await read_json_body(request, policy.max_request_bytes)
            current = clock()
            normalized = adapter.normalize(text, now=current)
            if (normalized.source_observed_at > current
                    or current - normalized.source_observed_at
                       > timedelta(seconds=policy.max_evidence_age_seconds)):
                raise BillingSourceDenied()
            checked = VerifiedBillingAttestation.model_validate_json(
                verifier.sign(normalized, now=current).model_dump_json(),
            )
        except BillingSourceDenied:
            return Response(status_code=409, content=b"", headers={"Cache-Control": "no-store"})
        except OverflowError:
            return Response(status_code=413, content=b"", headers={"Cache-Control": "no-store"})
        except TypeError:
            return Response(status_code=415, content=b"", headers={"Cache-Control": "no-store"})
        except Exception:
            return Response(status_code=400, content=b"", headers={"Cache-Control": "no-store"})
        return Response(
            checked.model_dump_json(), media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/audit/provider-costs")
    async def audit_provider_costs(request: Request) -> Response:
        if not authenticated(request):
            return Response(status_code=401, content=b"", headers={"Cache-Control": "no-store"})
        if audit_adapter is None:
            return Response(status_code=404, content=b"", headers={"Cache-Control": "no-store"})
        try:
            text = await read_json_body(request, policy.max_audit_request_bytes)
            current = clock()
            normalized = audit_adapter.normalize_audit(text, now=current)
            if (normalized.source_observed_at > current
                    or current - normalized.source_observed_at
                       > timedelta(seconds=policy.max_evidence_age_seconds)):
                raise BillingSourceDenied()
            checked = VerifiedProviderCostAudit.model_validate_json(
                verifier.sign_cost_audit(normalized, now=current).model_dump_json(),
            )
        except BillingSourceDenied:
            return Response(status_code=409, content=b"", headers={"Cache-Control": "no-store"})
        except OverflowError:
            return Response(status_code=413, content=b"", headers={"Cache-Control": "no-store"})
        except TypeError:
            return Response(status_code=415, content=b"", headers={"Cache-Control": "no-store"})
        except Exception:
            return Response(status_code=400, content=b"", headers={"Cache-Control": "no-store"})
        return Response(
            checked.model_dump_json(), media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return app
