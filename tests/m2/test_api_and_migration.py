"""Authenticated local operator HTTP and reproducible schema migration."""

from __future__ import annotations

from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from safe_agent.api import create_app
from safe_agent.orchestration.office_tasks import ApprovalInput
from safe_agent.security.principal import AccessDenied, SignedOperatorTokens

from .conftest import NOW, arrange


def test_operator_token_signature_and_expiry(kit):
    signer = SignedOperatorTokens(b"m2_local_auth_key_32_bytes_or_more_test_000000")
    token = signer.issue_for_trusted_operator(kit["manager"])
    assert signer.verify(token, now=NOW) == kit["manager"]
    with pytest.raises(AccessDenied):
        signer.verify(token[:-1] + ("x" if token[-1] != "x" else "y"), now=NOW)
    with pytest.raises(AccessDenied):
        signer.verify(token, now=NOW + timedelta(days=3))
    with pytest.raises(ValueError):
        SignedOperatorTokens(b"short")


def test_operator_api_prevents_cross_tenant_access_and_self_approval(kit):
    coordinator, state, _, _, proposal = arrange(kit, approval=True)
    pending = coordinator.propose(kit["principal"], "task_m2", proposal.model_dump_json(),
                                   expected_state_version=state.state_version, now=NOW)
    current = coordinator.inspect(kit["principal"], "task_m2", now=NOW)
    signer = SignedOperatorTokens(b"m2_local_auth_key_32_bytes_or_more_test_000000")
    app = create_app(coordinator, signer, clock=lambda: NOW)
    client = TestClient(app)
    assert client.get("/health").json()["mode"] == "M2_NO_PROVIDER_EXECUTION"
    assert client.get("/v1/tasks/task_m2").status_code == 401
    wrong = kit["manager"].model_copy(update={"organization_id": "org_another"})
    bad = signer.issue_for_trusted_operator(wrong)
    assert client.get("/v1/tasks/task_m2", headers={"authorization": "Bearer " + bad}).status_code == 404
    admin = signer.issue_for_trusted_operator(kit["manager"])
    visible = client.get("/v1/tasks/task_m2", headers={"authorization": "Bearer " + admin})
    assert visible.status_code == 200
    assert visible.json()["phase"] == "AWAITING_APPROVAL"
    audit = client.get("/v1/tasks/task_m2/events", headers={"authorization": "Bearer " + admin})
    assert audit.status_code == 200
    assert "cust_c1" not in audit.text and "cal_main_private" not in audit.text
    body = ApprovalInput(
        proposal_hash=pending.proposal_hash,
        expected_state_version=current.state_version, approved=True,
        expires_at=NOW + timedelta(hours=1),
    ).model_dump(mode="json")
    initiator = signer.issue_for_trusted_operator(kit["principal"])
    assert client.post("/v1/tasks/task_m2/approvals", json=body,
                       headers={"authorization": "Bearer " + initiator}).status_code == 404
    reply = client.post("/v1/tasks/task_m2/approvals", json=body,
                        headers={"authorization": "Bearer " + admin})
    assert reply.status_code == 200
    assert reply.json()["decision"] == "AUTHORIZED"
    assert "NO_PROVIDER_EFFECT" in reply.json()["warning"]
    assert client.post("/v1/tasks/task_m2/approvals", json=body,
                       headers={"authorization": "Bearer " + admin}).status_code == 409
    checked = client.get("/v1/tasks/task_m2/replay", headers={"authorization": "Bearer " + admin})
    assert checked.status_code == 200 and checked.json()["phase"] == "READY_TO_COMMIT"


def test_alembic_upgrade_schema_matches_models_on_isolated_sqlite(tmp_path, monkeypatch):
    db = tmp_path / "migrated.db"
    url = f"sqlite+pysqlite:///{db}"
    monkeypatch.setenv("SAFE_AGENT_DATABASE_URL", url)
    cfg = Config(str(__import__("pathlib").Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("script_location", str(__import__("pathlib").Path(__file__).resolve().parents[2] / "migrations"))
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        names = set(inspect(engine).get_table_names())
        assert {"safe_tasks", "safe_manifest_versions", "safe_projections",
                "safe_simulations", "safe_proposals", "safe_approvals",
                "safe_evidence_ledger", "safe_execution_records",
                "safe_protected_references"} <= names
    finally:
        engine.dispose()
