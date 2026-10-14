"""Locally tested canonicalization; upstream compatibility is not certified."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from safe_agent.canonical import canonical_json, sha256


def test_known_sha256_fixture() -> None:
    assert sha256({"a": 1}) == (
        "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )


def test_canonical_order_and_unicode_normalization() -> None:
    assert canonical_json({"b": "e\u0301", "a": [1, 2]}) == b'{"a":[1,2],"b":"\xc3\xa9"}'
    assert sha256({"b": "é", "a": [1, 2]}) == sha256({"a": [1, 2], "b": "e\u0301"})


def test_timezone_normalization_for_datetime_objects() -> None:
    instant = datetime(2026, 10, 15, 18, tzinfo=timezone.utc)
    offset = datetime.fromisoformat("2026-10-15T14:00:00-04:00")
    assert canonical_json(instant) == canonical_json(offset) == b'"2026-10-15T18:00:00Z"'


def test_no_binary_float_or_naive_datetime() -> None:
    for value in (float("nan"), 1.25, datetime(2026, 10, 15)):
        with pytest.raises((ValueError, TypeError)):
            canonical_json(value)


def test_nonfinite_decimal_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_json(Decimal("NaN"))


def test_canonical_nested_unicode_collision_fails() -> None:
    with pytest.raises(ValueError):
        canonical_json({"e\u0301": 1, "é": 2})
