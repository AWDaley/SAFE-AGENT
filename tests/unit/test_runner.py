"""M0 shell produces an uncommitted proposal only."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from safe_agent.agent import FakePlanner, SafeTaskContext
from safe_agent.contracts import Projection, RequestClarification, Task
from safe_agent.orchestration import TaskRunner


def _models(data: dict[str, object]) -> tuple[Task, SafeTaskContext, Projection]:
    return (
        Task.model_validate_json(json.dumps(data["task"])),
        SafeTaskContext.model_validate_json(json.dumps(data["context"])),
        Projection.model_validate_json(json.dumps(data["projection"])),
    )


def test_fake_planner_is_deterministic_and_never_completes(demo: dict[str, object]) -> None:
    task, context, projection = _models(demo)
    runner = TaskRunner(FakePlanner())
    first = runner.plan_once(task, context, projection)
    second = runner.plan_once(task, context, projection)
    assert first == second
    assert first.proposal_hash.startswith("sha256:")
    assert first.state == "PROPOSED_UNVERIFIED"
    assert first.explicitly_not_committed


def test_empty_calendar_requests_clarification(demo: dict[str, object]) -> None:
    modified = deepcopy(demo)
    assert isinstance(modified["projection"], dict)
    modified["projection"]["free_slots"] = []
    task, context, projection = _models(modified)
    result = TaskRunner(FakePlanner()).plan_once(task, context, projection)
    assert isinstance(result.proposal, RequestClarification)
    assert result.proposal.reason_code == "NO_AVAILABLE_SLOT"


def test_runner_rejects_cross_tenant_projection(demo: dict[str, object]) -> None:
    modified = deepcopy(demo)
    assert isinstance(modified["projection"], dict)
    modified["projection"]["organization_id"] = "org_other"
    task, context, projection = _models(modified)
    with pytest.raises(ValueError, match="cross-organization"):
        TaskRunner(FakePlanner()).plan_once(task, context, projection)


def test_runner_rejects_cross_task_context(demo: dict[str, object]) -> None:
    modified = deepcopy(demo)
    assert isinstance(modified["context"], dict)
    modified["context"]["task_id"] = "task_foreign"
    task, context, projection = _models(modified)
    with pytest.raises(ValueError, match="cross-task"):
        TaskRunner(FakePlanner()).plan_once(task, context, projection)


def test_runner_rejects_wrong_calendar_scope(demo: dict[str, object]) -> None:
    modified = deepcopy(demo)
    assert isinstance(modified["projection"], dict)
    modified["projection"]["calendar_ref"] = "calendar_foreign"
    task, context, projection = _models(modified)
    with pytest.raises(ValueError, match="calendar scope"):
        TaskRunner(FakePlanner()).plan_once(task, context, projection)
