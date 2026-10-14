"""M0 explicitly has no executable authority or provider-write path."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from safe_agent.agent import FakePlanner, SafeTaskContext
from safe_agent.contracts import Projection, Task
from safe_agent.contracts.control import DecisionStatus
from safe_agent.orchestration import TaskRunner
from safe_agent.tal import UnconfiguredTALAuthority


def test_unconfigured_tal_denies_every_calendar_proposal(demo: dict[str, object]) -> None:
    task = Task.model_validate_json(json.dumps(demo["task"]))
    context = SafeTaskContext.model_validate_json(json.dumps(demo["context"]))
    projection = Projection.model_validate_json(json.dumps(demo["projection"]))
    planned = TaskRunner(FakePlanner()).plan_once(task, context, projection)
    assert planned.proposal.operation == "calendar.create_event"
    decision = UnconfiguredTALAuthority().validate(task, planned.proposal)
    assert decision.decision is DecisionStatus.DENIED
    assert decision.reason_code == "TAL_OFFICE_DOMAIN_NOT_INTEGRATED"
    assert planned.explicitly_not_committed


def test_agent_and_planning_shell_never_import_privileged_modules() -> None:
    root = Path(__file__).resolve().parents[2] / "src/safe_agent"
    protected = [*list((root / "agent").rglob("*.py")), root / "orchestration/runner.py"]
    forbidden = ("safe_agent.tal", "safe_agent.execution", "safe_agent.adapters", "tal.runtime")
    for path in protected:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), (path, alias.name)
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(forbidden), (path, node.module)


def test_planner_context_contains_only_scoped_metadata(demo: dict[str, object]) -> None:
    context = SafeTaskContext.model_validate_json(json.dumps(demo["context"]))
    payload = context.model_dump_json()
    for prohibited in ("email", "password", "credential", "oauth", "provider_id", "legal_name"):
        assert prohibited not in payload
