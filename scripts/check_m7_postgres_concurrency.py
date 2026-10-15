"""M7 integration: real PostgreSQL concurrent inference budget admission.

CI-only, using an ephemeral PostgreSQL instance and synthetic micro-USD
amounts. This checks the real M7-03 SQL conditional-update path and Alembic
head; it NEVER calls a model provider or authorizes an office effect.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from safe_agent.storage.inference_budget import InferenceBudgetDenied, _change_account
from safe_agent.storage.models import InferenceBudgetAccountRow
from safe_agent.storage.repository import LedgerRepository

EXPECTED_HEAD = "0009_inference_cost_accounts"
EXPECTED_TRIGGERS = {
    "safe_inference_budget_events_immutable",
    "safe_inference_billing_attestations_immutable",
}
CALL_MICROUSD = 50
REQUESTS_EACH_TENANT = 8
PER_TENANT_CAP_MICROUSD = 150
GLOBAL_CAP_MICROUSD = 350


@dataclass(frozen=True)
class _Tx:
    """Minimal typed transaction adapter for the actual budget SQL helper."""

    session: Session


def main() -> None:
    url = os.environ.get("SAFE_AGENT_DATABASE_URL", "")
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("M7 concurrency acceptance requires isolated PostgreSQL")
    repo = LedgerRepository.production_engine(url)
    try:
        with repo.engine.connect() as connection:
            version_columns = inspect(connection).get_columns("alembic_version")
            if (len(version_columns) != 1
                    or version_columns[0]["name"] != "version_num"
                    or getattr(version_columns[0]["type"], "length", 0) < 128):
                raise AssertionError("Alembic revision tracking column remains too short")
            heads = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
            if heads != [EXPECTED_HEAD]:
                raise AssertionError("PostgreSQL is not at canonical PR #27 revision")
            triggers = set(connection.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE NOT tgisinternal AND tgname "
                    "LIKE 'safe_inference_%_immutable'"
                )
            ).scalars().all())
            if not EXPECTED_TRIGGERS.issubset(triggers):
                raise AssertionError("immutable inference events/attestations triggers absent")

        inspector = inspect(repo.engine)
        for name in (
            "safe_inference_budget_accounts",
            "safe_inference_budget_reservations",
            "safe_inference_budget_events",
            "safe_inference_billing_attestations",
        ):
            if not inspector.has_table(name):
                raise AssertionError("canonical inference accounting table missing")
        if inspector.has_table("safe_inference_budget_scopes"):
            raise AssertionError("alternative PR #29 migration has been installed")

        # This exercise deliberately tests actual conditional SQL admission,
        # not a Python counter or SQLite emulation. All scopes are updated
        # in one transaction: a refusal must roll back every scope.
        with repo._sessions.begin() as session:
            session.add_all([
                InferenceBudgetAccountRow(
                    scope_key=key, kind=kind, limit_microusd=limit,
                    held_microusd=0, charged_microusd=0, version=0,
                )
                for key, kind, limit in (
                    ("global", "GLOBAL", GLOBAL_CAP_MICROUSD),
                    ("tenant_one", "TENANT", PER_TENANT_CAP_MICROUSD),
                    ("tenant_two", "TENANT", PER_TENANT_CAP_MICROUSD),
                )
            ])

        def attempt(tenant: str) -> bool:
            try:
                with repo._sessions.begin() as session:
                    tx = _Tx(session)
                    _change_account(
                        tx, "global", hold_delta=CALL_MICROUSD, charge_delta=0
                    )
                    _change_account(
                        tx, tenant, hold_delta=CALL_MICROUSD, charge_delta=0
                    )
                return True
            except InferenceBudgetDenied:
                return False

        calls = (
            ["tenant_one"] * REQUESTS_EACH_TENANT
            + ["tenant_two"] * REQUESTS_EACH_TENANT
        )
        with ThreadPoolExecutor(max_workers=12) as executor:
            accepted = list(executor.map(attempt, calls))
        by_tenant = {
            tenant: sum(ok for t, ok in zip(calls, accepted, strict=True) if t == tenant)
            for tenant in ("tenant_one", "tenant_two")
        }
        if by_tenant != {"tenant_one": 3, "tenant_two": 3}:
            raise AssertionError("concurrent tenant budget overspend or lost admission")
        if sum(accepted) != 6:
            raise AssertionError("concurrent global budget was not conservative")

        with repo._sessions() as session:
            rows = {
                row.scope_key: row
                for row in session.scalars(select(InferenceBudgetAccountRow)).all()
            }
            for name, expected_held, expected_version in (
                ("global", 300, 6),
                ("tenant_one", 150, 3),
                ("tenant_two", 150, 3),
            ):
                row = rows[name]
                if (row.held_microusd != expected_held
                        or row.charged_microusd != 0
                        or row.version != expected_version
                        or row.held_microusd + row.charged_microusd
                           > row.limit_microusd):
                    raise AssertionError("Postgres inference budget accounting mismatch")

        # A denied caller cannot leave a partial global reservation.
        with repo._sessions.begin() as session:
            tx = _Tx(session)
            try:
                _change_account(
                    tx, "global", hold_delta=CALL_MICROUSD, charge_delta=0
                )
                _change_account(
                    tx, "tenant_one", hold_delta=CALL_MICROUSD, charge_delta=0
                )
            except InferenceBudgetDenied:
                session.rollback()
            else:
                raise AssertionError("concurrent account-limit bypass admitted")
        with repo._sessions() as session:
            global_row = session.get(InferenceBudgetAccountRow, "global")
            if global_row is None or global_row.held_microusd != 300:
                raise AssertionError("failed multi-account hold did not roll back")

        print("M7_PR27_POSTGRES_CONCURRENT_BUDGET_ADMISSION=PASS")
    finally:
        repo.close()


if __name__ == "__main__":
    main()
