"""Fail-closed M7-03/04/05a migration-lineage and interface integration gate.

The PR #27 cost-account migration is the ONE canonical successor to 0008.
PR #29's alternative 0009_inference_budget must never be combined with it.
0010 is the sole reviewed successor; 0009 remains content-pinned.
No production connection is opened; optional DB verification is SQLite-only.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from inspect import signature
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from safe_agent.billing.verifier import OfflineSyntheticBillingVerifier
from safe_agent.contracts.inference_budget import BillingChargeAttestation
from safe_agent.storage.inference_budget import InferenceBudgetLedger
from safe_agent.storage.models import Base

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEAD = "0010_inference_dispatch"
CANONICAL_BUDGET = "0009_inference_cost_accounts"
EXPECTED_PARENT = "0008_planning_workspace"
ALTERNATIVE_HEAD = "0009_inference_budget"
CANONICAL_BUDGET_SHA256 = "5af5edacd8a8b1b72969452dd02437a295335c7cc8567714b398872813052fdb"
REQUIRED_COLUMNS = {
    "safe_inference_invocations": {
        "request_id", "organization_id", "task_id", "attempt_id", "manifest_hash",
        "request_digest", "binding_hash", "route_id", "model_revision", "prompt_digest",
        "task_state_version", "workspace_version", "issued_at", "deadline_at",
        "lease_expires_at", "state", "state_version", "claim_version", "claim_owner",
        "claim_until", "dispatch_started_at", "proposal_hash", "reason_code",
        "event_sequence", "latest_event_hash", "updated_at",
    },
    "safe_inference_invocation_events": {
        "request_id", "sequence", "binding_hash", "previous_state", "state",
        "occurred_at", "reason_code", "proposal_hash", "preceding_hash", "event_hash",
    },
    "safe_inference_budget_accounts": {
        "scope_key", "kind", "limit_microusd", "held_microusd",
        "charged_microusd", "version",
    },
    "safe_inference_budget_reservations": {
        "request_id", "organization_id", "task_id", "route_id",
        "model_revision", "prompt_digest", "backend_kind",
        "max_microusd", "held_microusd", "charged_microusd",
        "status", "event_sequence", "latest_event_hash",
    },
    "safe_inference_budget_events": {
        "request_id", "sequence", "event_kind", "held_microusd",
        "charged_microusd", "event_hash",
    },
    "safe_inference_billing_attestations": {
        "receipt_id", "request_id", "issuer_id", "charge_microusd",
        "source_evidence_hash", "attestation_digest",
    },
}


def check_migration_and_contract() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if set(heads) != {EXPECTED_HEAD} or len(heads) != 1:
        raise RuntimeError("M7 budget migration conflict: expected exactly one canonical head")
    head = script.get_revision(EXPECTED_HEAD)
    if head is None or head.down_revision != CANONICAL_BUDGET:
        raise RuntimeError("M7 invocation migration must directly extend the canonical budget")
    migration = script.get_revision(CANONICAL_BUDGET)
    if migration is None or migration.down_revision != EXPECTED_PARENT:
        raise RuntimeError("M7 budget migration does not follow 0008_planning_workspace")

    files = sorted(
        p.name for p in (ROOT / "migrations" / "versions").glob("0009_*.py")
    )
    if files != [CANONICAL_BUDGET + ".py"] or (ALTERNATIVE_HEAD + ".py") in files:
        raise RuntimeError("conflicting M7 0009 budget migration detected")

    frozen = ROOT / "migrations" / "versions" / (CANONICAL_BUDGET + ".py")
    if hashlib.sha256(frozen.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != CANONICAL_BUDGET_SHA256:
        raise RuntimeError("canonical 0009 was rewritten; use a reviewed forward migration")

    for table_name, needed in REQUIRED_COLUMNS.items():
        table = Base.metadata.tables.get(table_name)
        if table is None or not needed.issubset(set(table.columns.keys())):
            raise RuntimeError("M7-04 incompatible with PR #27 budget SQL schema")
    if "safe_inference_budget_scopes" in Base.metadata.tables:
        raise RuntimeError("PR #29 alternative inference budget ORM must not be installed")
    fields = set(BillingChargeAttestation.model_fields)
    needed_fields = {
        "receipt_id", "request_id", "organization_id", "task_id",
        "route_id", "model_revision", "prompt_digest", "charge_microusd",
        "source_evidence_hash", "issuer_id", "observed_at", "signature",
    }
    if not needed_fields.issubset(fields):
        raise RuntimeError("M7-04 verifier is incompatible with PR #27 charge contract")
    ledger_params = signature(InferenceBudgetLedger.reconcile_attested).parameters
    verifier_params = signature(OfflineSyntheticBillingVerifier.attest).parameters
    if (
        "evidence" not in ledger_params
        or "verifier_secret" not in ledger_params
        or "now" not in ledger_params
        or "record" not in verifier_params
        or "now" not in verifier_params
    ):
        raise RuntimeError("M7-04 billing verifier no longer matches PR #27 interface")


def check_migrated_test_database() -> None:
    """Inspect only an explicitly configured *SQLite test fixture* database."""
    url = os.environ.get("SAFE_AGENT_DATABASE_URL", "")
    if not url.startswith("sqlite+pysqlite:///"):
        raise RuntimeError("only an explicitly configured SQLite test DB can be inspected")
    engine = create_engine(url)
    try:
        actual_tables = set(inspect(engine).get_table_names())
        if "safe_inference_budget_scopes" in actual_tables:
            raise RuntimeError("PR #29 alternative budget tables detected")
        for table_name, required in REQUIRED_COLUMNS.items():
            if table_name not in actual_tables:
                raise RuntimeError("PR #27 budget table is missing")
            actual_columns = {
                item["name"] for item in inspect(engine).get_columns(table_name)
            }
            if not required.issubset(actual_columns):
                raise RuntimeError("migrated SQL columns mismatch PR #27 canonical schema")
        with engine.connect() as conn:
            revisions = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if revisions != [EXPECTED_HEAD]:
            raise RuntimeError("SQLite migration head does not match reviewed 0010")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check canonical M7 budget migration")
    parser.add_argument(
        "--verify-database", action="store_true",
        help="also verify migrated SQLite test tables and alembic_version",
    )
    args = parser.parse_args()
    check_migration_and_contract()
    if args.verify_database:
        check_migrated_test_database()
    print("M7_CANONICAL_PR27_BUDGET_LINEAGE=PASS")


if __name__ == "__main__":
    main()
