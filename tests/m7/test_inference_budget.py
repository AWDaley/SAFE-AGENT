"""M7-03: durable, conservative inference authorization and usage accounting."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.inference import (
    InferenceReportedUsage,
    InferenceRequest,
    InferenceRoutePolicy,
)
from safe_agent.contracts.inference_budget import InferenceBudgetLimits
from safe_agent.contracts.office import AvailableSlot, Projection
from safe_agent.inference import (
    InferenceDenied,
    InferenceGateway,
    OfflineMockInferenceTransport,
)
from safe_agent.storage.inference_budget import InferenceBudgetDenied, InferenceBudgetLedger
from safe_agent.storage.models import InferenceBudgetEventRow, InferenceBudgetReservationRow
from safe_agent.storage.repository import LedgerRepository
from tests.m2.conftest import NOW

RATES = {
    "input_microusd_per_million_tokens": 1_000_000,
    "output_microusd_per_million_tokens": 1_000_000,
    "max_estimated_cost_microusd": 10_000,
}


def profile(**override):
    values = {
        "profile_id": "budget_demo",
        "global_max_microusd": 100_000,
        "tenant_max_microusd": 50_000,
        "task_max_microusd": 25_000,
        "global_max_calls": 20,
        "tenant_max_calls": 10,
        "task_max_calls": 8,
        "global_max_inflight": 5,
        "tenant_max_inflight": 4,
        "task_max_inflight": 2,
    }
    return InferenceBudgetLimits(**{**values, **override})


def price(**override):
    return InferenceRoutePolicy(
        route_id="route_billing_mock", model_revision="mock_v1",
        **{**RATES, **override},
    )


def register(kit):
    kit["coordinator"].register(
        kit["principal"], kit["task"], kit["manifest"], now=NOW,
    )


def task_prompt(kit, *, task_id=None):
    task_id = task_id or kit["task"].task_id
    ctx = SafeTaskContext(
        task_id=task_id, organization_id=kit["task"].organization_id,
        customer_ref=kit["customer_ref"], calendar_ref=kit["calendar_ref"],
        allowed_start=kit["manifest"].allowed_start,
        allowed_end=kit["manifest"].allowed_end,
        duration_minutes=30, time_zone=kit["manifest"].time_zone,
        template_id=kit["manifest"].template_id,
        simulation_ref="simulation_pending",
    )
    projection = Projection(
        task_id=task_id, organization_id=ctx.organization_id,
        calendar_ref=ctx.calendar_ref, revision="rev_budget_test",
        fetched_at=NOW, expires_at=NOW + timedelta(minutes=5),
        free_slots=(AvailableSlot(
            start_at=ctx.allowed_start,
            end_at=ctx.allowed_start + timedelta(hours=2),
        ),),
    )
    return canonical_json({
        "instruction": PLANNER_INSTRUCTION,
        "task": ctx, "availability": projection,
    }).decode()


def request(kit, *, serial=1, when=NOW, due=None):
    text = task_prompt(kit)
    import json
    payload = json.loads(text)
    return InferenceRequest(
        request_id="inf_" + f"{serial:032x}",
        route_id="route_billing_mock",
        model_revision="mock_v1",
        issued_at=when,
        deadline_at=due or when + timedelta(seconds=10),
        prompt_digest=sha256({
            "instruction": PLANNER_INSTRUCTION,
            "task": SafeTaskContext.model_validate_json(json.dumps(payload["task"])),
            "availability": Projection.model_validate_json(
                json.dumps(payload["availability"]),
            ),
        }),
        task=SafeTaskContext.model_validate_json(json.dumps(payload["task"])),
        availability=Projection.model_validate_json(json.dumps(payload["availability"])),
    )


def initialized(kit, *, limits=None):
    ledger = InferenceBudgetLedger(kit["repo"], limits or profile())
    ledger.configure(now=NOW)
    return ledger


def invoice(kit, ledger, *, policy=None, backend=None, clock=lambda: NOW):
    return InferenceGateway(
        policy or price(),
        backend if backend is not None else OfflineMockInferenceTransport(),
        clock=clock,
        budget=ledger,
    ).infer(task_prompt(kit))


def test_budget_configuration_is_explicit_and_cannot_reset_spending(kit):
    register(kit)
    engine = InferenceBudgetLedger(kit["repo"], profile())
    with pytest.raises(InferenceBudgetDenied):
        engine.reserve(request(kit), price(), now=NOW)
    engine.configure(now=NOW)
    engine.configure(now=NOW)
    reservation = engine.reserve(request(kit), price(), now=NOW)
    assert reservation.status == "RESERVED"
    assert reservation.version == 1
    assert reservation.maximum_held_microusd > 0
    with pytest.raises(InferenceBudgetDenied):
        engine.reserve(request(kit), price(), now=NOW)
    modified = InferenceBudgetLedger(kit["repo"], profile(global_max_microusd=10_000))
    with pytest.raises(InferenceBudgetDenied):
        modified.configure(now=NOW)
    assert engine.scope_view("global").calls_used == 1


def test_success_records_unverified_usage_and_never_releases_funds(kit):
    register(kit)
    ledger = initialized(kit)
    outcome = invoice(kit, ledger)
    assert outcome.provider_effect_performed is False
    assert outcome.model_authorized_execution is False
    assert outcome.reported_usage is not None
    with kit["repo"]._sessions() as session:
        row = session.scalars(select(InferenceBudgetReservationRow)).one()
        events = session.scalars(
            select(InferenceBudgetEventRow)
            .where(InferenceBudgetEventRow.request_id == row.request_id)
            .order_by(InferenceBudgetEventRow.ordinal),
        ).all()
        assert row.status == "RECORDED_UNVERIFIED"
        assert len(events) == 2
        assert [e.kind for e in events] == ["RESERVED", "RECORDED_UNVERIFIED"]
        req_id = row.request_id
    _ = req_id
    stats = ledger.scope_view("global")
    assert stats.held_microusd > 0
    assert stats.active_calls == 0
    assert stats.calls_used == 1
    assert ledger.scope_view("org:org_demo").held_microusd == stats.held_microusd
    assert ledger.scope_view("task:task_m2").held_microusd == stats.held_microusd
    assert outcome.reported_usage.independently_verified is False


def test_success_report_is_idempotent_and_mismatched_replay_rejected(kit):
    register(kit)
    ledger = initialized(kit)
    req = request(kit)
    first = ledger.reserve(req, price(), now=NOW)
    usage = InferenceReportedUsage(
        input_tokens=100, output_tokens=90, source="provider_reported",
    )
    finished = ledger.finish(
        req, now=NOW, usage=usage,
        proposal_digest="sha256:" + "d" * 64,
    )
    assert finished.status == "RECORDED_UNVERIFIED"
    assert finished.maximum_held_microusd == first.maximum_held_microusd
    assert finished.reported_estimated_microusd == 190
    assert finished.funds_released is False
    assert ledger.finish(
        req, now=NOW, usage=usage,
        proposal_digest="sha256:" + "d" * 64,
    ) == finished
    with pytest.raises(InferenceBudgetDenied):
        ledger.finish(
            req, now=NOW, usage=None,
            proposal_digest="sha256:" + "d" * 64,
        )
    with pytest.raises(InferenceBudgetDenied):
        ledger.uncertain(req, reason="BACKEND_EXCEPTION", now=NOW)
    assert ledger.receipt(req) == finished


def test_backend_failure_retains_max_charge_without_leaking_provider_error(kit):
    register(kit)
    ledger = initialized(kit, limits=profile(
        global_max_calls=1, tenant_max_calls=1, task_max_calls=1,
    ))

    class BrokenBackend:
        def infer(self, request):
            raise ValueError("GOOGLE_REFRESH_TOKEN PRIVATE")

    with pytest.raises(InferenceDenied, match="INFERENCE_BACKEND_UNAVAILABLE") as e:
        invoice(kit, ledger, backend=BrokenBackend())
    assert "GOOGLE_REFRESH_TOKEN" not in str(e.value)
    with kit["repo"]._sessions() as db:
        entry = db.scalars(select(InferenceBudgetReservationRow)).one()
        assert entry.status == "UNCERTAIN"
        assert entry.uncertainty_reason == "BACKEND_EXCEPTION"
    scope = ledger.scope_view("global")
    assert scope.calls_used == 1
    assert scope.held_microusd > 0
    assert scope.active_calls == 0
    with pytest.raises(InferenceDenied, match="INFERENCE_BUDGET_EXCEEDED"):
        invoice(kit, ledger)
    assert ledger.scope_view("global").calls_used == 1


def test_invalid_model_proposal_is_uncertain_and_never_refunded(kit):
    register(kit)
    ledger = initialized(kit)

    class Malicious:
        def infer(self, req):
            reply = OfflineMockInferenceTransport().infer(req)
            return reply.model_copy(update={
                "output_json": '{"operation":"shell.run","exec":"dump-private"}',
            })

    with pytest.raises(InferenceDenied, match="INFERENCE_RESPONSE_REJECTED"):
        invoice(kit, ledger, backend=Malicious())
    with kit["repo"]._sessions() as db:
        row = db.scalars(select(InferenceBudgetReservationRow)).one()
        assert row.status == "UNCERTAIN"
        assert row.uncertainty_reason == "INVALID_RESPONSE"
    assert ledger.scope_view("global").held_microusd > 0


def test_overreported_usage_freezes_all_future_calls(kit):
    register(kit)
    ledger = initialized(kit)

    class Overreported:
        def infer(self, req):
            reply = OfflineMockInferenceTransport().infer(req)
            return reply.model_copy(update={
                "reported_usage": InferenceReportedUsage(
                    input_tokens=100_000, output_tokens=0,
                    source="provider_reported",
                ),
            })

    with pytest.raises(InferenceDenied, match="INFERENCE_BUDGET_EXCEEDED"):
        invoice(kit, ledger, backend=Overreported())
    assert ledger.scope_view("global").requires_review is True
    assert ledger.scope_view("global").held_microusd > 0
    with pytest.raises(InferenceDenied, match="INFERENCE_BUDGET_EXCEEDED"):
        invoice(kit, ledger)


def test_zero_price_mock_still_enforces_call_count(kit):
    register(kit)
    ledger = initialized(kit, limits=profile(
        global_max_microusd=0, tenant_max_microusd=0, task_max_microusd=0,
        task_max_calls=1,
    ))
    policy = InferenceRoutePolicy(
        route_id="route_billing_mock", model_revision="mock_v1",
    )
    assert invoice(kit, ledger, policy=policy).proposal.operation == "calendar.create_event"
    assert ledger.scope_view("global").held_microusd == 0
    assert ledger.scope_view("task:task_m2").calls_used == 1
    with pytest.raises(InferenceDenied, match="INFERENCE_BUDGET_EXCEEDED"):
        invoice(kit, ledger, policy=policy)


def test_task_and_global_caps_block_further_backend_calls(kit):
    register(kit)
    ledger = initialized(kit, limits=profile(
        task_max_calls=1, global_max_calls=3,
    ))

    class CountingMock:
        calls = 0
        def infer(self, req):
            self.calls += 1
            return OfflineMockInferenceTransport().infer(req)

    backend = CountingMock()
    invoice(kit, ledger, backend=backend)
    with pytest.raises(InferenceDenied, match="INFERENCE_BUDGET_EXCEEDED"):
        invoice(kit, ledger, backend=backend)
    assert backend.calls == 1
    assert ledger.scope_view("global").calls_used == 1


def test_expired_lease_recovers_after_sql_reopen_but_money_stays_held(kit, tmp_path):
    register(kit)
    limits = profile(task_max_calls=2, task_max_inflight=1)
    ledger = initialized(kit, limits=limits)
    original = request(kit, serial=1, due=NOW + timedelta(seconds=10))
    amount = ledger.reserve(original, price(), now=NOW).maximum_held_microusd
    assert ledger.scope_view("global").active_calls == 1

    restarted = LedgerRepository.testing_sqlite(
        f"sqlite+pysqlite:///{tmp_path / 'm2.sqlite'}",
    )
    try:
        fresh = InferenceBudgetLedger(restarted, limits)
        fresh.configure(now=NOW + timedelta(seconds=11))
        assert fresh.recover(now=NOW + timedelta(seconds=11)) == 1
        evidence = fresh.receipt(original)
        assert evidence.status == "UNCERTAIN"
        assert evidence.uncertainty_reason == "EXPIRED_UNRECONCILED"
        assert evidence.maximum_held_microusd == amount
        scope = fresh.scope_view("global")
        assert scope.held_microusd == amount
        assert scope.active_calls == 0
        assert scope.calls_used == 1
        next_req = request(
            kit, serial=2, when=NOW + timedelta(seconds=12),
            due=NOW + timedelta(seconds=20),
        )
        fresh.reserve(next_req, price(), now=NOW + timedelta(seconds=12))
        assert fresh.scope_view("global").held_microusd == 2 * amount
        assert fresh.scope_view("global").calls_used == 2
    finally:
        restarted.close()


def test_late_success_remains_uncertain_with_full_charge(kit):
    register(kit)
    ledger = initialized(kit)
    clock = [NOW]

    class Delayed:
        def infer(self, request):
            clock[0] += timedelta(seconds=15)
            return OfflineMockInferenceTransport().infer(request)

    with pytest.raises(InferenceDenied, match="INFERENCE_DEADLINE_EXCEEDED"):
        invoice(kit, ledger, backend=Delayed(), clock=lambda: clock[0])
    with kit["repo"]._sessions() as session:
        record = session.scalars(select(InferenceBudgetReservationRow)).one()
        assert record.status == "UNCERTAIN"
        assert record.uncertainty_reason == "LATE_RESPONSE"
    assert ledger.scope_view("global").active_calls == 0
    assert ledger.scope_view("global").held_microusd > 0


def test_usage_overrun_reported_to_store_freezes_budget(kit):
    register(kit)
    ledger = initialized(kit)
    req = request(kit)
    ledger.reserve(req, price(), now=NOW)
    result = ledger.finish(
        req, now=NOW, proposal_digest="sha256:" + "a" * 64,
        usage=InferenceReportedUsage(
            input_tokens=10_000, output_tokens=0,
            source="provider_reported",
        ),
    )
    assert result.status == "UNCERTAIN"
    assert result.uncertainty_reason == "REPORTED_USAGE_OVERRUN"
    assert ledger.scope_view("global").requires_review
    assert ledger.receipt(req) == result


def test_untrusted_request_cannot_charge_a_different_tenant_or_task(kit):
    register(kit)
    ledger = initialized(kit)
    original = request(kit)
    forged_task = original.task.model_copy(update={"organization_id": "org_other"})
    forged_projection = original.availability.model_copy(
        update={"organization_id": "org_other"},
    )
    wrong = InferenceRequest(
        request_id="inf_" + "a" * 32,
        route_id=original.route_id, model_revision=original.model_revision,
        issued_at=original.issued_at, deadline_at=original.deadline_at,
        prompt_digest=sha256({
            "instruction": PLANNER_INSTRUCTION,
            "task": forged_task, "availability": forged_projection,
        }),
        task=forged_task, availability=forged_projection,
    )
    with pytest.raises(InferenceBudgetDenied):
        ledger.reserve(wrong, price(), now=NOW)
    assert ledger.scope_view("global").calls_used == 0


def test_sql_event_tamper_or_mutable_reservation_tamper_refused(kit):
    register(kit)
    ledger = initialized(kit)
    req = request(kit)
    ledger.reserve(req, price(), now=NOW)
    assert ledger.receipt(req).status == "RESERVED"
    with kit["repo"]._sessions.begin() as db:
        db.execute(
            update(InferenceBudgetEventRow)
            .where(InferenceBudgetEventRow.request_id == req.request_id)
            .values(event_hash="sha256:" + "f" * 64),
        )
    with pytest.raises(InferenceBudgetDenied):
        ledger.receipt(req)


def test_budget_enforcement_across_parallel_sqlite_writers(kit):
    register(kit)
    ledger = initialized(kit, limits=profile(
        global_max_calls=1, tenant_max_calls=1, task_max_calls=1,
        global_max_inflight=2, tenant_max_inflight=2, task_max_inflight=2,
    ))

    def attempt(serial):
        try:
            return ledger.reserve(request(kit, serial=serial), price(), now=NOW)
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        answers = list(executor.map(attempt, (21, 22)))
    valid = [x for x in answers if not isinstance(x, Exception)]
    assert len(valid) == 1
    assert ledger.scope_view("global").calls_used == 1
    assert ledger.scope_view("global").active_calls == 1
    assert ledger.scope_view("task:task_m2").held_microusd > 0


def test_mock_gateway_default_without_budget_remains_compatible(kit):
    register(kit)
    outcome = InferenceGateway(
        InferenceRoutePolicy(
            route_id="route_billing_mock", model_revision="mock_v1",
        ),
        OfflineMockInferenceTransport(), clock=lambda: NOW,
    ).infer(task_prompt(kit))
    assert outcome.provider_effect_performed is False


def test_automatic_budget_denial_leaves_no_provider_effects(kit):
    register(kit)
    ledger = initialized(kit, limits=profile(task_max_microusd=0))
    class SpyBackend:
        def __init__(self):
            self.calls = 0
        def infer(self, req):
            self.calls += 1
            return OfflineMockInferenceTransport().infer(req)
    backend = SpyBackend()
    with pytest.raises(InferenceDenied, match="INFERENCE_BUDGET_EXCEEDED"):
        invoice(kit, ledger, backend=backend)
    assert backend.calls == 0
    assert ledger.scope_view("global").calls_used == 0
