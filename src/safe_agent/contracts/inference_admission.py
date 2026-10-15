"""Trusted admission metadata, never a dispatch or business authorization."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from safe_agent.contracts.base import StrictModel
from safe_agent.contracts.inference import InferenceRoutePolicy
from safe_agent.contracts.inference_budget import InferenceBudgetLimits, InferenceReservationReceipt
from safe_agent.contracts.inference_dispatch import InferenceInvocationBinding, InferenceInvocationView


class InferenceThroughputLimits(StrictModel):
    """One conservative rolling window and outstanding-invocation caps."""

    window_seconds: int = Field(default=60, ge=1, le=3600)
    global_requests: int = Field(default=60, ge=1, le=10000)
    tenant_requests: int = Field(default=20, ge=1, le=10000)
    task_requests: int = Field(default=3, ge=1, le=10)
    global_outstanding: int = Field(default=10, ge=1, le=1000)
    tenant_outstanding: int = Field(default=4, ge=1, le=1000)
    task_outstanding: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def ordered(self) -> InferenceThroughputLimits:
        if not (self.task_requests <= self.tenant_requests <= self.global_requests
                and self.task_outstanding <= self.tenant_outstanding <= self.global_outstanding):
            raise ValueError("ADMISSION_LIMIT_ORDER_DENIED")
        return self


class InferenceAdmissionPolicy(StrictModel):
    """Host-only, write-once profile shared by every coordinated caller.

    M7-05b admits metadata only. Every hold is conservatively REMOTE/unverified;
    no caller can request the synthetic refund path, even with a zero price.
    """

    schema_version: Literal["safe_agent.inference_admission_policy.v1"] = (
        "safe_agent.inference_admission_policy.v1"
    )
    route: InferenceRoutePolicy
    budget: InferenceBudgetLimits
    throughput: InferenceThroughputLimits = Field(default_factory=InferenceThroughputLimits)
    mode: Literal["OFFLINE_ADMISSION_ONLY"] = "OFFLINE_ADMISSION_ONLY"


class InferenceAdmissionReceipt(StrictModel):
    """Trusted caller receipt. Not exposed to a model or accepted by a writer."""

    schema_version: Literal["safe_agent.inference_admission_receipt.v1"] = (
        "safe_agent.inference_admission_receipt.v1"
    )
    disposition: Literal["CREATED", "EXISTING"]
    binding: InferenceInvocationBinding
    invocation: InferenceInvocationView
    reservation: InferenceReservationReceipt
    dispatch_authorized: Literal[False] = False
    provider_effect_performed: Literal[False] = False
    synthetic_only: Literal[True] = True

    @model_validator(mode="after")
    def bound(self) -> InferenceAdmissionReceipt:
        if (self.binding.request_id != self.invocation.request_id
                or self.binding.request_id != self.reservation.request_id
                or self.binding.task_id != self.invocation.task_id
                or self.binding.task_id != self.reservation.task_id):
            raise ValueError("ADMISSION_RECEIPT_BINDING_DENIED")
        return self
