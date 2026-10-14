"""Self-contained deterministic M4 demonstration with process restart and reconciliation.

Requires no network, real provider, credentials, LLM or user files.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.broker import BrokerScope, ReferenceBroker, ResourceKind
from safe_agent.canonical import sha256
from safe_agent.contracts.actions import CalendarCreateEvent
from safe_agent.contracts.control import Task, TaskBudget, TaskManifest
from safe_agent.contracts.office import SimulatedChange
from safe_agent.execution.recovery import FaultPoint, InjectedCrash, SyntheticRecoveryWorker
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.security.principal import Principal
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.storage.repository import LedgerRepository
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice
from safe_agent.virtual_office.models import ObserveCalendar


TEST_BROKER_KEY = b"M4_demo_synthetic_records_only_hmac_key_1122334455"


def run_m4_demo() -> dict[str, object]:
    now = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
    start = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
    end = start + timedelta(minutes=30)
    fixture = (Path(__file__).resolve().parents[3]
               / "fixtures/synthetic_office/office_state.json")
    source = SyntheticOfficeState.from_fixture(fixture)
    with TemporaryDirectory(prefix="safe_agent_m4_synthetic_") as temporary:
        db = f"sqlite+pysqlite:///{Path(temporary) / 'recovery.sqlite'}"
        repo = LedgerRepository.testing_sqlite(db)
        try:
            broker = ReferenceBroker(secret=TEST_BROKER_KEY)
            scope = BrokerScope(
                organization_id="org_demo", initiator_id="employee_demo",
                task_id="task_m4_demo", operation="calendar.create_event", now=now,
            )
            exp = now + timedelta(days=2)
            calendar_ref = broker.mint(
                resource_id="cal_main_private", resource_kind=ResourceKind.CALENDAR,
                scope=scope, permitted_operations=("calendar.create_event",), expires_at=exp,
            )
            customer_ref = broker.mint(
                resource_id="cust_c1", resource_kind=ResourceKind.CUSTOMER,
                scope=scope, permitted_operations=("calendar.create_event",), expires_at=exp,
            )
            office = VirtualOffice(source, broker)
            provider = DurableFakeProvider(repo.engine, source)
            adapter = FakeCalendarAdapter(source, durable_store=provider)
            gate = OfficeGate(broker)
            principal = Principal(
                organization_id="org_demo", actor_id="employee_demo",
                authenticated_until=exp,
                scopes=("task:create", "task:prepare", "task:propose", "task:read",
                        "task:audit", "task:execute"),
            )
            manifest = TaskManifest(
                manifest_id="manifest_m4_demo", manifest_version=1, task_id="task_m4_demo",
                organization_id="org_demo", initiator_id="employee_demo",
                calendar_ref=calendar_ref, customer_ref=customer_ref,
                permitted_operations=("calendar.create_event",),
                allowed_start=start, allowed_end=start + timedelta(hours=3),
                time_zone="America/New_York", template_id="follow_up_v1",
                budget=TaskBudget(max_cycles=2),
            )
            task = Task(
                task_id="task_m4_demo", organization_id="org_demo",
                initiator_id="employee_demo", manifest_id=manifest.manifest_id,
                manifest_hash=sha256(manifest), objective_code="schedule_follow_up",
                created_at=now - timedelta(minutes=10), deadline=exp,
            )
            gateway = SyntheticExecutionGateway(repo, office, gate, broker, adapter)
            coordinator = TrustedOfficeTasks(repo, office, gate, synthetic_gateway=gateway)
            coordinator.register(principal, task, manifest, now=now)
            projection = office.observe(ObserveCalendar(
                organization_id="org_demo", initiator_id="employee_demo",
                task_id=task.task_id, calendar_ref=calendar_ref,
                customer_ref=customer_ref,
                from_at=start, until_at=start + timedelta(hours=3),
            ), as_of=now)
            preview = office.simulate(projection, SimulatedChange(
                calendar_ref=calendar_ref, customer_ref=customer_ref,
                start_at=start, end_at=end, template_id="follow_up_v1",
            ), actor="employee_demo", customer_ref=customer_ref, now=now)
            coordinator.preview(principal, task.task_id, projection, preview,
                                expected_state_version=0, now=now)
            proposal = CalendarCreateEvent(
                calendar_ref=calendar_ref, customer_ref=customer_ref,
                start_at=start, end_at=end, time_zone="America/New_York",
                template_id="follow_up_v1", simulation_ref=preview.simulation_ref,
                projection_rev=projection.revision,
            )
            coordinator.propose(principal, task.task_id, proposal.model_dump_json(),
                                expected_state_version=1, now=now)
            before = adapter.reader().snapshot(
                calendar_id="cal_main_private", organization_id="org_demo",
            )
            action_hash = gateway.enqueue(
                principal, task.task_id,
                expected_state_version=repo.read_task(task.task_id).state_version, now=now,
            )
            worker = SyntheticRecoveryWorker(repo, broker, adapter)
            crashed = False
            try:
                worker.process_one(task_id=task.task_id, now=now,
                                   fault=FaultPoint.AFTER_PROVIDER_COMMIT)
            except InjectedCrash:
                crashed = True
            if not crashed:
                raise AssertionError("synthetic fault injection did not interrupt the worker")
        finally:
            repo.close()
        # Entire ledger repository and fake provider are reconstructed from disk.
        repo2 = LedgerRepository.testing_sqlite(db)
        try:
            provider2 = DurableFakeProvider(repo2.engine, source)
            adapter2 = FakeCalendarAdapter(source, durable_store=provider2)
            worker2 = SyntheticRecoveryWorker(
                repo2, ReferenceBroker(secret=TEST_BROKER_KEY), adapter2,
            )
            receipt = worker2.process_one(task_id=task.task_id,
                                          now=now + timedelta(seconds=31))
            if receipt is None:
                raise AssertionError("durable action not recoverable after restart")
            after = adapter2.reader().snapshot(
                calendar_id="cal_main_private", organization_id="org_demo",
            )
            snapshot = repo2.replay_task(task.task_id)
            return {
                "mode": "M4_SYNTHETIC_CRASH_RECOVERY_NO_REAL_PROVIDER",
                "upstream_tal_calendar_domain_certified": False,
                "injected_crash": crashed,
                "action_hash": action_hash,
                "before_booking_count": len(before.calendar.bookings),
                "after_booking_count": len(after.calendar.bookings),
                "durable_provider_effects": adapter2.effect_count,
                "outbox": repo2.read_outbox(task.task_id),
                "receipt": receipt.model_dump(mode="json"),
                "replayed_phase": snapshot.phase.value,
                "audit_event_count": len(repo2.read_events(task.task_id)),
            }
        finally:
            repo2.close()


def print_m4_demo() -> None:
    print(json.dumps(run_m4_demo(), indent=2))
