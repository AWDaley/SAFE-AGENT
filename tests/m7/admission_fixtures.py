"""Synthetic, registered task and real planning-lease fixtures; no model calls."""

from datetime import timedelta
from uuid import uuid4

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.control import Task, TaskBudget, TaskManifest
from safe_agent.contracts.inference import InferenceRoutePolicy
from safe_agent.contracts.inference_admission import InferenceAdmissionPolicy, InferenceThroughputLimits
from safe_agent.contracts.inference_budget import InferenceBudgetLimits
from safe_agent.contracts.office import AvailableSlot, Projection
from safe_agent.security.principal import Principal
from safe_agent.storage.planning import PlanningWorkspaceStore
from tests.m2.conftest import NOW


def policy(**throughput):
    return InferenceAdmissionPolicy(
        route=InferenceRoutePolicy(
            route_id="admission_route", model_revision="offline_v1",
            max_estimated_cost_microusd=1000,
            output_microusd_per_million_tokens=100000,
        ),
        budget=InferenceBudgetLimits(
            global_microusd=10000, tenant_microusd=5000,
            task_microusd=2000, max_call_microusd=1000,
        ),
        throughput=InferenceThroughputLimits(**throughput),
    )


def assigned(repo, *, org="org_admission", now=NOW, ident=None):
    ident = ident or uuid4().hex
    principal = Principal(
        organization_id=org, actor_id="actor_" + org,
        scopes=("task:create", "task:prepare", "task:propose", "task:read"),
        authenticated_until=now + timedelta(hours=1),
    )
    start = now + timedelta(days=1)
    manifest = TaskManifest(
        manifest_id="manifest_" + ident, manifest_version=1,
        organization_id=org, initiator_id=principal.actor_id, task_id="task_" + ident,
        calendar_ref="calendar_" + ident, customer_ref="customer_" + ident,
        permitted_operations=("calendar.create_event",),
        allowed_start=start, allowed_end=start + timedelta(hours=2),
        max_duration_minutes=60, time_zone="America/New_York",
        template_id="follow_up_v1", budget=TaskBudget(max_cycles=2), requires_approval=True,
    )
    task = Task(
        task_id=manifest.task_id, organization_id=org, initiator_id=principal.actor_id,
        manifest_id=manifest.manifest_id, manifest_hash=sha256(manifest),
        objective_code="schedule_follow_up", created_at=now, deadline=now + timedelta(hours=1),
    )
    repo.create_task(task, manifest, actor=principal.actor_id, now=now)
    store = PlanningWorkspaceStore(repo)
    lease = store.begin(principal, task.task_id, expected_task_version=0,
                        expected_workspace_version=0, mode="text_only", now=now)
    context = SafeTaskContext(
        task_id=task.task_id, organization_id=org, calendar_ref=manifest.calendar_ref,
        customer_ref=manifest.customer_ref, allowed_start=manifest.allowed_start,
        allowed_end=manifest.allowed_end, duration_minutes=30,
        time_zone=manifest.time_zone, template_id=manifest.template_id,
        simulation_ref="simulation_pending",
    )
    projection = Projection(
        task_id=task.task_id, organization_id=org, calendar_ref=manifest.calendar_ref,
        revision="projection_" + ident, fetched_at=now, expires_at=now + timedelta(minutes=5),
        free_slots=(AvailableSlot(start_at=manifest.allowed_start, end_at=manifest.allowed_end),),
    )
    prompt = canonical_json({"instruction": PLANNER_INSTRUCTION,
                             "task": context, "availability": projection}).decode()
    return principal, lease, prompt
