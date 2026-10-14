"""Closed, sanitized planner transport. This adapter has no provider tools.

The injected generate callback is a *text-only* model transport chosen by an
operator; SAFE-AGENT never passes it a resolver, credential or write interface.
Every reply must be exactly one closed-schema proposal, not a tool invocation.
"""

from __future__ import annotations

from typing import Callable, Literal

from safe_agent.canonical import canonical_json
from safe_agent.contracts.actions import (
    CalendarCreateEvent,
    RequestClarification,
    parse_proposal_json,
)
from safe_agent.contracts.office import Projection

from .fake import FakePlanner
from .interfaces import Planner, SafeTaskContext


class ClosedJsonPlanner:
    def __init__(self, generate: Callable[[str], str]) -> None:
        self._generate = generate

    def propose(
        self, context: SafeTaskContext, projection: Projection,
    ) -> CalendarCreateEvent | RequestClarification:
        # Type constraints are applied before model transport is invoked. Only
        # minimized free/busy data and scoped handles cross this boundary.
        payload = canonical_json({
            "instruction": "Return exactly one valid SAFE-AGENT v1 proposal JSON object. "
                           "Do not claim task completion. No tools are available.",
            "task": context,
            "availability": projection,
        }).decode("utf-8")
        response = self._generate(payload)
        if not isinstance(response, str):
            raise ValueError("model transport must return one proposal JSON string")
        return parse_proposal_json(response)


def select_planner(mode: Literal["fake", "json"], *,
                   generate: Callable[[str], str] | None = None) -> Planner:
    if mode == "fake":
        return FakePlanner()
    if mode == "json" and generate is not None:
        return ClosedJsonPlanner(generate)
    raise ValueError("json planner requires an explicitly configured text-only transport")
