"""Pin the separate provider observation and trusted test intent contracts."""

from __future__ import annotations

import argparse
from pathlib import Path

from safe_agent.canonical import canonical_json
from safe_agent.integrations.google_calendar.contracts import (
    GoogleCalendarObservation,
    GoogleTestEventIntent,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "schemas/safe-agent-m5-contracts-v1.json"


def export() -> bytes:
    return canonical_json({
        "contract_set": "safe_agent.m5.google_test.v1",
        "note": "Test-only OAuth/availability contracts; live model writes blocked pending TAL.",
        "schemas": {
            "test_calendar_observation": GoogleCalendarObservation.model_json_schema(),
            "trusted_test_event_intent": GoogleTestEventIntent.model_json_schema(),
        },
    }) + b"\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = export()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != expected:
            raise SystemExit("M5 schema drift: regenerate and review")
        print("M5 schema export matches pinned bytes")
    else:
        TARGET.write_bytes(expected)
        print(TARGET)


if __name__ == "__main__":
    main()
