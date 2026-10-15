"""Mandatory in the dedicated PostgreSQL CI job; no SQLite substitutes.

Local default runs skip this module unless the explicit disposable-DB flag is
set. With that flag set, absent/incorrect PostgreSQL configuration fails.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from safe_agent.canonical import sha256
from safe_agent.contracts.inference_dispatch import seal_invocation_event
from safe_agent.storage.models import (
    InferenceBudgetReservationRow,
    InferenceInvocationEventRow,
    InferenceInvocationRow,
)
from tests.m7.dispatch_fixtures import NOW, binding, initial_values, seed_parents

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_DISPATCH") != "1",
    reason="requires the explicit disposable PostgreSQL dispatch acceptance job",
)
INVOCATIONS = InferenceInvocationRow.__table__
EVENTS = InferenceInvocationEventRow.__table__


@pytest.fixture
def pg():
    url = os.environ.get("SAFE_AGENT_DATABASE_URL", "")
    assert url.startswith("postgresql+psycopg://"), "real isolated PostgreSQL is mandatory"
    engine = create_engine(url)
    assert engine.dialect.name == "postgresql"
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def connection(pg):
    with pg.connect() as conn:
        tx = conn.begin()
        try:
            yield conn
        finally:
            tx.rollback()


def advance(conn, bound, previous, state, *, at, reason=None, proposal=None):
    event = seal_invocation_event(
        bound, state=state, previous=previous, occurred_at=at,
        reason_code=reason, proposal_hash=proposal,
    )
    values = {
        "state": event.state, "state_version": event.sequence - 1,
        "event_sequence": event.sequence, "latest_event_hash": event.event_hash,
        "reason_code": event.reason_code, "proposal_hash": event.proposal_hash,
        "updated_at": event.occurred_at,
    }
    if state == "DISPATCH_STARTED":
        values["dispatch_started_at"] = event.occurred_at
    result = conn.execute(update(INVOCATIONS).where(
        INVOCATIONS.c.request_id == bound.request_id,
        INVOCATIONS.c.state_version == previous.sequence - 1,
    ).values(**values))
    assert result.rowcount == 1
    conn.execute(EVENTS.insert().values(**event.model_dump(exclude={"schema_version"})))
    return event


def test_postgres_migrated_head_composite_bindings_and_triggers_are_present(connection):
    assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [
        "0010_inference_dispatch",
    ]
    triggers = set(connection.execute(text(
        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
    )).scalars())
    assert {
        "safe_inference_invocation_guard", "safe_inference_invocation_events_immutable",
        "safe_inference_budget_events_immutable", "safe_inference_billing_attestations_immutable",
    } <= triggers
    assert len(inspect(connection).get_foreign_keys(INVOCATIONS.name)) == 3


def test_postgres_binding_and_event_history_are_immutable(connection):
    bound = binding(uuid4().hex)
    seed_parents(connection, bound)
    connection.execute(INVOCATIONS.insert().values(**initial_values(bound)))
    first = seal_invocation_event(bound, state="RESERVED", occurred_at=NOW)
    connection.execute(EVENTS.insert().values(**first.model_dump(exclude={"schema_version"})))
    for changes in (
        {"request_digest": sha256({"tampered": True})}, {"workspace_version": 2},
        {"binding_hash": sha256({"forged": True})}, {"issued_at": NOW - timedelta(seconds=1)},
    ):
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(update(INVOCATIONS).where(
                INVOCATIONS.c.request_id == bound.request_id,
            ).values(**changes))
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(INVOCATIONS.delete().where(INVOCATIONS.c.request_id == bound.request_id))
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(EVENTS.delete().where(EVENTS.c.request_id == bound.request_id))
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(EVENTS.update().where(EVENTS.c.request_id == bound.request_id).values(
            event_hash=sha256({"changed": True}),
        ))


def test_postgres_unknown_cannot_rewind_or_refund_and_dispatch_marker_is_frozen(connection):
    bound = binding(uuid4().hex)
    seed_parents(connection, bound)
    connection.execute(INVOCATIONS.insert().values(**initial_values(bound)))
    first = seal_invocation_event(bound, state="RESERVED", occurred_at=NOW)
    connection.execute(EVENTS.insert().values(**first.model_dump(exclude={"schema_version"})))
    started = advance(connection, bound, first, "DISPATCH_STARTED", at=NOW + timedelta(seconds=1))
    unknown = advance(connection, bound, started, "UNKNOWN", at=NOW + timedelta(seconds=20),
                      reason="DISPATCH_OUTCOME_UNKNOWN")
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(INVOCATIONS.update().where(INVOCATIONS.c.request_id == bound.request_id)
                           .values(state="RESERVED", state_version=3, event_sequence=4,
                                   latest_event_hash=sha256({"resend": True}), reason_code=None,
                                   dispatch_started_at=None, updated_at=NOW + timedelta(seconds=21)))
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(INVOCATIONS.update().where(INVOCATIONS.c.request_id == bound.request_id)
                           .values(dispatch_started_at=NOW + timedelta(seconds=2)))
    assert connection.execute(select(INVOCATIONS.c.state).where(
        INVOCATIONS.c.request_id == bound.request_id,
    )).scalar_one() == unknown.state
    assert connection.execute(select(InferenceBudgetReservationRow.held_microusd).where(
        InferenceBudgetReservationRow.request_id == bound.request_id,
    )).scalar_one() == 10


def test_postgres_claim_requires_new_fence_and_stale_state_compare_loses(connection):
    bound = binding(uuid4().hex)
    seed_parents(connection, bound)
    connection.execute(INVOCATIONS.insert().values(**initial_values(bound)))
    connection.execute(INVOCATIONS.update().where(INVOCATIONS.c.request_id == bound.request_id)
                       .values(claim_owner="worker_one", claim_until=NOW + timedelta(seconds=5),
                               claim_version=1))
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(INVOCATIONS.update().where(INVOCATIONS.c.request_id == bound.request_id)
                           .values(claim_owner="worker_two", claim_version=1))
    first = seal_invocation_event(bound, state="RESERVED", occurred_at=NOW)
    started = advance(connection, bound, first, "DISPATCH_STARTED", at=NOW + timedelta(seconds=1))
    assert started.sequence == 2
    stale = connection.execute(INVOCATIONS.update().where(
        INVOCATIONS.c.request_id == bound.request_id, INVOCATIONS.c.state_version == 0,
    ).values(claim_version=2))
    assert stale.rowcount == 0


def test_postgres_simultaneous_attempt_inserts_have_one_winner_and_keep_existing_holds(pg):
    task_key = uuid4().hex
    a = binding(uuid4().hex, task_key=task_key, attempt_key=task_key)
    b = binding(uuid4().hex, task_key=task_key, attempt_key=task_key)
    with pg.begin() as conn:
        seed_parents(conn, a)
        seed_parents(conn, b, task=False, attempt=False)

    def insert(bound):
        try:
            with pg.begin() as conn:
                conn.execute(INVOCATIONS.insert().values(**initial_values(bound)))
            return True
        except IntegrityError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = list(pool.map(insert, (a, b)))
    assert sorted(admitted) == [False, True]
    with pg.connect() as conn:
        assert len(conn.execute(select(INVOCATIONS.c.request_id).where(
            INVOCATIONS.c.attempt_id == "attempt_" + task_key,
        )).scalars().all()) == 1
        assert conn.execute(select(InferenceBudgetReservationRow.held_microusd).where(
            InferenceBudgetReservationRow.request_id.in_((a.request_id, b.request_id)),
        )).scalars().all() == [10, 10]


def test_postgres_foreign_scope_and_initial_success_rows_are_denied(connection):
    bound = binding(uuid4().hex)
    seed_parents(connection, bound)
    for changes in (
        {"organization_id": "another_org"}, {"task_state_version": 99},
        {"route_id": "another_route"},
        {"state": "RESPONSE_VALIDATED", "state_version": 2, "event_sequence": 3,
         "dispatch_started_at": NOW, "proposal_hash": sha256({"fabricated": "success"})},
    ):
        with pytest.raises(DBAPIError), connection.begin_nested():
            connection.execute(INVOCATIONS.insert().values(**{**initial_values(bound), **changes}))
