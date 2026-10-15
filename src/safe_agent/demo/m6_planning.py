"""Disposable demonstration of durable text-only planning, without model networks.

A deterministic callback stands in for an LLM transport to demonstrate the
exact closed JSON boundary. No provider write, OAuth account or secret is used.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from safe_agent.agent.json_planner import ClosedJsonPlanner
from safe_agent.broker import ReferenceBroker
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.intake import TaskIntakeRequest
from safe_agent.contracts.planning import PlanningCommand
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.orchestration.persistent_planning import TrustedPersistentPlanning
from safe_agent.orchestration.synthetic_workflow import TrustedSyntheticWorkflow
from safe_agent.orchestration.task_provisioning import (
    TrustedSyntheticOption,
    TrustedTaskProvisioner,
)
from safe_agent.security.principal import Principal
from safe_agent.storage.planning import PlanningWorkspaceStore
from safe_agent.storage.repository import LedgerRepository
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice


def run_m6_planning_demo() -> dict[str, object]:
    """Create and safely plan a task using a simulated text-only model reply."""
    now = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
    fixture = (Path(__file__).resolve().parents[3]
               / "fixtures/synthetic_office/office_state.json")
    source = SyntheticOfficeState.from_fixture(fixture)
    messages: list[str] = []

    def text_only_synthetic_stub(text: str) -> str:
        messages.append(text)
        safe = json.loads(text)
        context = safe["task"]
        opening = safe["availability"]["free_slots"][0]
        start = datetime.fromisoformat(opening["start_at"])
        return CalendarCreateEvent(
            calendar_ref=context["calendar_ref"], customer_ref=context["customer_ref"],
            start_at=start, end_at=start + timedelta(minutes=context["duration_minutes"]),
            time_zone=context["time_zone"], template_id=context["template_id"],
            simulation_ref=context["simulation_ref"],
            projection_rev=safe["availability"]["revision"],
        ).model_dump_json()

    with TemporaryDirectory(prefix="safe_agent_m6_planning_") as temporary:
        db = f"sqlite+pysqlite:///{Path(temporary) / 'planning.sqlite'}"
        repo = LedgerRepository.testing_sqlite(db)
        try:
            broker = ReferenceBroker(
                secret=b"M6_safe_planning_demo_synthetic_fixture_key_not_for_production",
            )
            office = VirtualOffice(source, broker)
            coordinator = TrustedOfficeTasks(repo, office, OfficeGate(broker))
            options = (TrustedSyntheticOption(
                option_id="opt_demo_planning",
                organization_id="org_demo", initiator_id="employee_demo",
                workflow="schedule_follow_up",
                customer_resource_id="cust_c1",
                calendar_resource_id="cal_main_private",
                appointment_template_id="follow_up_v1",
                time_zone="America/New_York",
                requires_approval=True,
            ),)
            provisioner = TrustedTaskProvisioner(
                coordinator, repo, broker, source, options,
            )
            principal = Principal(
                organization_id="org_demo", actor_id="employee_demo",
                scopes=("task:create", "task:prepare", "task:propose", "task:read"),
                authenticated_until=now + timedelta(days=2),
            )
            created = provisioner.create(
                principal, TaskIntakeRequest(
                    option_id="opt_demo_planning",
                    idempotency_key="req_" + "e" * 32,
                    allowed_start=now + timedelta(days=1),
                    allowed_end=now + timedelta(days=1, hours=2),
                ), now=now,
            )
            workflow = TrustedSyntheticWorkflow(repo, office, broker, coordinator)
            planner = TrustedPersistentPlanning(
                PlanningWorkspaceStore(repo, max_attempts=3), workflow,
                planner=ClosedJsonPlanner(text_only_synthetic_stub), clock=lambda: now,
            )
            result = planner.plan(
                principal, created.task_id,
                PlanningCommand(expected_state_version=0, expected_workspace_version=0),
                now=now,
            )
            replayed = repo.replay_task(created.task_id)
            if (result.status != "PREPARED" or result.attempts_used != 1
                    or replayed.phase is not OfficePhase.AWAITING_APPROVAL
                    or replayed.external_effects != 0 or len(messages) != 1):
                raise AssertionError("bounded synthetic model planning not recorded")
            if any(private in messages[0] for private in (
                "cal_main_private", "cust_c1", "oauth", "customer_name",
            )):
                raise AssertionError("private synthetic source appeared in planning prompt")
            return {
                "mode": "M6_PERSISTENT_TEXT_ONLY_SIMULATED",
                "workspace_status": result.status,
                "model_stub_calls": len(messages),
                "planning_attempts": result.attempts_used,
                "max_planning_attempts": result.max_attempts,
                "task_phase": replayed.phase.value,
                "external_effects": replayed.external_effects,
                "live_provider_writes_enabled": False,
                "m5_live_acceptance": "BLOCKED",
            }
        finally:
            repo.close()


def print_m6_planning_demo() -> None:
    print(json.dumps(run_m6_planning_demo(), indent=2))
