"""Synthetic fixtures are intentionally free of real contact information."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def demo() -> dict[str, object]:
    path = Path(__file__).resolve().parents[1] / "fixtures/synthetic_office/demo.json"
    return json.loads(path.read_text(encoding="utf-8"))
