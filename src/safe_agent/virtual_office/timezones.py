"""Calendar-local wall time conversion with explicit DST ambiguity rejection."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from safe_agent.contracts.base import require_aware


class CalendarTimeError(ValueError):
    """Nonexistent, ambiguous or calendar-inconsistent wall-clock time."""


def checked_local_time(day: date, local_time: time, zone: ZoneInfo) -> datetime:
    """Reject transitions rather than silently choosing a DST fold or gap."""
    if local_time.tzinfo is not None:
        raise CalendarTimeError("office hours must be local wall-clock times")
    wall = datetime.combine(day, local_time)
    valid: list[datetime] = []
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold)
        roundtrip = candidate.astimezone(UTC).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == wall and not any(
            candidate.utcoffset() == prior.utcoffset() for prior in valid
        ):
            valid.append(candidate)
    if not valid:
        raise CalendarTimeError("nonexistent local wall time (DST gap)")
    if len(valid) > 1:
        raise CalendarTimeError("ambiguous local wall time (DST fold)")
    return valid[0]


def checked_instant(value: datetime, zone: ZoneInfo) -> datetime:
    """The ISO offset must agree with an unambiguous wall time in the named zone.

    A nonexistent or folded wall time is rejected even when an offset was
    supplied. M1 deliberately requires clarification instead of guessing.
    """
    require_aware(value)
    wall = value.replace(tzinfo=None)
    checked = checked_local_time(wall.date(), wall.time(), zone)
    if value.utcoffset() != checked.utcoffset():
        raise CalendarTimeError("offset disagrees with calendar time zone")
    return checked
