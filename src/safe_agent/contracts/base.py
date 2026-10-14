"""Strict, immutable contracts shared across trust boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class StrictModel(BaseModel):
    """Reject unknown fields and coercions; do not mutate parsed records."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, validate_default=True)


def require_aware(value: datetime) -> datetime:
    """Naive timestamps are not permitted even in local/synthetic workflows."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value
