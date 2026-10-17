"""OpenAI organization Costs API capture adapter.

The official Costs API is aggregate (daily buckets, optionally grouped by project
and line item). This adapter therefore emits only aggregate audit evidence and can
never produce request-level NormalizedBillingEvidence.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field, field_validator, model_validator

from safe_agent.billing.audit_adapters import ProviderCostAuditAdapter
from safe_agent.billing.verifier import BillingSourceDenied
from safe_agent.canonical import sha256
from safe_agent.contracts.base import StrictModel, require_aware
from safe_agent.contracts.provider_billing_audit import NormalizedProviderCostAudit

_DAY = 86_400


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate OpenAI cost evidence key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid OpenAI cost numeric constant")


class _Money(StrictModel):
    value: Decimal = Field(ge=0)
    currency: Literal["usd"]


class _CostResult(StrictModel):
    object: Literal["organization.costs.result"]
    amount: _Money
    line_item: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=128)

    @field_validator("line_item", "project_id")
    @classmethod
    def printable(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid provider label")
        return value


class _Bucket(StrictModel):
    object: Literal["bucket"]
    start_time: int = Field(ge=0)
    end_time: int = Field(gt=0)
    results: list[_CostResult] = Field(max_length=100_000)

    @model_validator(mode="after")
    def daily(self) -> _Bucket:
        if self.end_time - self.start_time != _DAY:
            raise ValueError("OpenAI costs bucket must be exactly one UTC day")
        return self


class _CostsPage(StrictModel):
    object: Literal["page"]
    data: list[_Bucket] = Field(max_length=180)
    has_more: bool
    next_page: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def pagination_shape(self) -> _CostsPage:
        if self.has_more != (self.next_page is not None):
            raise ValueError("OpenAI costs pagination shape denied")
        if self.next_page is not None and (
            not self.next_page.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in self.next_page)
        ):
            raise ValueError("OpenAI costs cursor denied")
        return self


class _PageCapture(StrictModel):
    request_page: str | None = Field(default=None, max_length=512)
    response: _CostsPage


class OpenAICostsCapture(StrictModel):
    """Credential-free capture prepared by a separate trusted OpenAI Admin fetcher."""

    schema_version: Literal["safe_agent.openai_costs_capture.v1"] = (
        "safe_agent.openai_costs_capture.v1"
    )
    provider: Literal["openai"] = "openai"
    endpoint: Literal["/v1/organization/costs"] = "/v1/organization/costs"
    start_time: int = Field(ge=0)
    end_time: int = Field(gt=0)
    bucket_width: Literal["1d"] = "1d"
    group_by: list[Literal["project_id", "line_item"]] = Field(
        min_length=2, max_length=2,
    )
    fetched_at: datetime
    pages: list[_PageCapture] = Field(min_length=1, max_length=180)

    @field_validator("fetched_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def bounded_query(self) -> OpenAICostsCapture:
        if (
            self.end_time <= self.start_time
            or self.start_time % _DAY != 0
            or self.end_time % _DAY != 0
            or (self.end_time - self.start_time) // _DAY > 180
            or set(self.group_by) != {"project_id", "line_item"}
            or len(self.group_by) != 2
        ):
            raise ValueError("OpenAI costs query boundary denied")
        return self


def _decimal_string(value: Decimal) -> str:
    try:
        if not value.is_finite() or value < 0:
            raise ValueError
        normalized = format(value, "f")
        whole, dot, fraction = normalized.partition(".")
        fraction = fraction.rstrip("0")
        if len(fraction) > 12:
            raise ValueError("OpenAI cost precision exceeds audit contract")
        result = whole + (dot + fraction if fraction else "")
        if len(whole) > 16:
            raise ValueError("OpenAI cost amount too large")
        return result
    except (InvalidOperation, ValueError):
        raise BillingSourceDenied() from None


class OpenAIOrganizationCostsAdapter(ProviderCostAuditAdapter):
    """Normalize a complete captured Costs API response into aggregate audit evidence."""

    @property
    def adapter_id(self) -> str:
        return "openai_organization_costs_v1"

    def normalize_audit(
        self, payload: str, *, now: datetime,
    ) -> NormalizedProviderCostAudit:
        current = require_aware(now).astimezone(UTC)
        try:
            json.loads(
                payload,
                parse_float=Decimal,
                parse_int=int,
                object_pairs_hook=_unique,
                parse_constant=_reject_constant,
            )
            capture = OpenAICostsCapture.model_validate_json(payload)
            fetched = capture.fetched_at.astimezone(UTC)
            if fetched > current:
                raise ValueError("future provider capture")

            previous_cursor: str | None = None
            buckets: list[_Bucket] = []
            for index, page in enumerate(capture.pages):
                if page.request_page != previous_cursor:
                    raise ValueError("OpenAI costs page cursor chain denied")
                response = page.response
                buckets.extend(response.data)
                if index + 1 < len(capture.pages):
                    if not response.has_more or response.next_page is None:
                        raise ValueError("OpenAI costs capture ended before next page")
                    previous_cursor = response.next_page
                else:
                    if response.has_more or response.next_page is not None:
                        raise ValueError("OpenAI costs capture is incomplete")

            expected = capture.start_time
            total = Decimal(0)
            projects: set[str] = set()
            line_items: set[str] = set()
            results = 0
            for bucket in buckets:
                if bucket.start_time != expected or bucket.end_time != expected + _DAY:
                    raise ValueError("OpenAI costs buckets are missing, duplicated or unordered")
                expected = bucket.end_time
                for item in bucket.results:
                    total += item.amount.value
                    results += 1
                    if item.project_id is not None:
                        projects.add(item.project_id)
                    if item.line_item is not None:
                        line_items.add(item.line_item)
            if expected != capture.end_time:
                raise ValueError("OpenAI costs capture does not cover requested window")

            return NormalizedProviderCostAudit(
                provider="openai",
                source_adapter_id=self.adapter_id,
                source_evidence_hash=sha256(capture),
                source_observed_at=fetched,
                window_start=datetime.fromtimestamp(capture.start_time, UTC),
                window_end=datetime.fromtimestamp(capture.end_time, UTC),
                total_usd_decimal=_decimal_string(total),
                bucket_count=len(buckets),
                result_count=results,
                project_ids=tuple(sorted(projects)),
                line_items=tuple(sorted(line_items)),
            )
        except BillingSourceDenied:
            raise
        except Exception:
            raise BillingSourceDenied() from None
