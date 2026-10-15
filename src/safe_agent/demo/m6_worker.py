"""Isolated M6 supervised synthetic-worker demonstration, with no provider network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter
from safe_agent.broker import ReferenceBroker
from safe_agent.contracts.intake import TaskIntakeRequest
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.execution.supervisor import (
    SupervisedSyntheticWorker,
    SyntheticSupervisorPolicy,
)
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.orchestration.synthetic_workflow import TrustedSyntheticWorkflow
from safe_agent.orchestration.task_provisioning import (
    TrustedSyntheticOption,
    TrustedTaskProvisioner,
)
from safe_agent.security.principal import Principal
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.storage.repository import LedgerRepository
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice


def run_m6_worker_demo() -> dict[str, object]:
    """Exercise assignment -> preparation -> queue -> supervised verified effect."""
    now = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
    fixture = (Path(__file__).resolve().parents[3]
               / "fixtures/synthetic_office/office_state.json")
    source = SyntheticOfficeState.from_fixture(fixture)
    with TemporaryDirectory(prefix="safe_agent_m6_worker_synthetic_") as temporary:
        repo = LedgerRepository.testing_sqlite(
            f"sqlite+pysqlite:///{Path(temporary) / 'worker.sqlite'}",
        )
        try:
            broker = ReferenceBroker(
                secret=b"M6_supervised_worker_demo_fake_secret_only_never_production",
            )
            office = VirtualOffice(source, broker)
            gate = OfficeGate(broker)
            coordinator = TrustedOfficeTasks(repo, office, gate)
            provisioner = TrustedTaskProvisioner(
                coordinator, repo, broker, source, (
                    TrustedSyntheticOption(
                        option_id="opt_m6_worker_demo",
                        organization_id=source.organization_id,
                        initiator_id="employee_demo",
                        workflow="create_internal_task",
                        customer_resource_id="cust_c1",
                        task_code="review_request",
                        time_zone="America/New_York",
                        requires_approval=False,
                    ),
                ),
            )
            principal = Principal(
                organization_id=source.organization_id, actor_id="employee_demo",
                scopes=(
                    "task:create", "task:prepare", "task:propose",
                    "task:read", "task:audit", "task:execute",
                ),
                authenticated_until=now + timedelta(days=2),
            )
            created = provisioner.create(
                principal, TaskIntakeRequest(
                    option_id="opt_m6_worker_demo",
                    idempotency_key="req_" + "d" * 32,
                    allowed_start=now + timedelta(days=1),
                    allowed_end=now + timedelta(days=1, hours=2),
                ), now=now,
            )
            provider = DurableFakeProvider(repo.engine, source)
            calendar = FakeCalendarAdapter(source, durable_store=provider)
            internal_tasks = FakeInternalTaskAdapter(source, durable_store=provider)
            gateway = SyntheticExecutionGateway(
                repo, office, gate, broker, calendar, task_adapter=internal_tasks,
            )
            workflow = TrustedSyntheticWorkflow(
                repo, office, broker, coordinator, gateway=gateway,
            )
            prepared = workflow.prepare(
                principal, created.task_id, expected_state_version=created.state_version,
                now=now,
            )
            workflow.queue(
                principal, created.task_id, expected_state_version=prepared.state_version,
                now=now,
            )
            # A separate trusted component, not the API or model, runs the worker.
            worker = SyntheticRecoveryWorker(
                repo, broker, calendar, task_adapter=internal_tasks, office=office,
            )
            supervisor = SupervisedSyntheticWorker(
                worker,
                policy=SyntheticSupervisorPolicy(
                    max_claims_per_pass=2, idle_poll_seconds=0.01,
                ),
                clock=lambda: now,
            )
            report = supervisor.serve(Event(), max_passes=1)
            final = repo.replay_task(created.task_id)
            if (final.phase is not OfficePhase.COMPLETED
                    or final.external_effects != 1
                    or report.verified != 1 or internal_tasks.effect_count != 1):
                raise AssertionError("supervised fake-provider verification failed")
            return {
                "mode": "M6_SUPERVISED_SYNTHETIC_ONLY",
                "supervisor": report.model_dump(mode="json"),
                "task_phase": final.phase.value,
                "verified_effects": final.external_effects,
                "fake_provider_effects": internal_tasks.effect_count,
                "real_provider_writes_enabled": False,
                "m5_live_acceptance": "BLOCKED",
            }
        finally:
            repo.close()


def print_m6_worker_demo() -> None:
    print(json.dumps(run_m6_worker_demo(), indent=2))
