"""Provisional SHA-256 canonicalization mirroring TAL's documented c14n-v0.2.

This is for M0 *non-authoritative* artifact IDs only. It is NOT a verified TAL
implementation replacement; see docs/tal-compatibility.md.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

CANONICALIZATION_ID = "tal-json-c14n-0.2-provisional"


def canonical_data(value: object) -> object:
    if isinstance(value, BaseModel):
        return canonical_data(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("naive datetime cannot be canonicalized")
        normalized = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return normalized.replace(".000000Z", "Z")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal")
        formatted = format(value.normalize(), "f")
        return "0" if formatted in {"-0", ""} else formatted
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, tuple | list):
        return [canonical_data(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        normalized_mapping: dict[str, object] = {}
        for key, item in value.items():
            name = unicodedata.normalize("NFC", key)
            if name in normalized_mapping:
                raise ValueError("duplicate key after Unicode normalization")
            normalized_mapping[name] = canonical_data(item)
        return normalized_mapping
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        raise TypeError("binary float is forbidden in TAL canonical JSON")
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        canonical_data(value), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()
