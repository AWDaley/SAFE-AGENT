"""Standalone STDIN/STDOUT protocol fixture for a no-network model container.

This file intentionally imports Python stdlib only and cannot reference the
SAFE-AGENT broker, database, provider clients, environment credentials or tools.
It is a deterministic worker stub, NOT an actual large language model.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta


def _aware(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.utcoffset() is None:
        raise ValueError("offset required")
    return value


def respond(source: bytes) -> bytes:
    if not 0 < len(source) <= 16_384:
        raise ValueError("bounded input required")
    request = json.loads(source.decode("utf-8", errors="strict"))
    if not isinstance(request, dict) or set(request) != {
        "instruction", "task", "availability",
    }:
        raise ValueError("unexpected planner message")
    context = request["task"]
    projection = request["availability"]
    if (not isinstance(context, dict) or not isinstance(projection, dict)
            or set(projection) != {
                "schema_version", "organization_id", "task_id",
                "calendar_ref", "revision", "source", "fetched_at",
                "expires_at", "free_slots",
            } or context.get("task_id") != projection.get("task_id")
            or context.get("organization_id") != projection.get("organization_id")
            or context.get("calendar_ref") != projection.get("calendar_ref")):
        raise ValueError("scope denied")
    earliest = _aware(context["allowed_start"])
    latest = _aware(context["allowed_end"])
    duration = int(context["duration_minutes"])
    if not 1 <= duration <= 60:
        raise ValueError("invalid duration")
    answer = None
    for slot in sorted(
        projection["free_slots"], key=lambda value: value["start_at"],
    ):
        start = max(earliest, _aware(slot["start_at"]))
        end = start + timedelta(minutes=duration)
        if end <= min(latest, _aware(slot["end_at"])):
            answer = {
                "schema_version": "safe_agent.proposal.v1",
                "operation": "calendar.create_event",
                "calendar_ref": context["calendar_ref"],
                "customer_ref": context["customer_ref"],
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "time_zone": context["time_zone"],
                "template_id": context["template_id"],
                "simulation_ref": context["simulation_ref"],
                "projection_rev": projection["revision"],
            }
            break
    if answer is None:
        answer = {
            "schema_version": "safe_agent.proposal.v1",
            "operation": "agent.request_clarification",
            "reason_code": "NO_AVAILABLE_SLOT",
            "question": "No permitted synthetic opening is available.",
        }
    output = json.dumps(
        answer, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if len(output) > 16_384:
        raise ValueError("bounded output required")
    return output


def main() -> int:
    try:
        payload = sys.stdin.buffer.read(16_385)
        output = respond(payload)
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except (ValueError, TypeError, KeyError, UnicodeError):
        # No traceback, source text, private identifiers or environment data.
        return 2


if __name__ == "__main__":
    sys.exit(main())
