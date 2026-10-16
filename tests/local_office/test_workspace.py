"""Local workspace lifecycle tests; no customer data or network providers."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete

from safe_agent.local_office.application import issue_session, principal_for
from safe_agent.local_office.state import initialize, open_office
from safe_agent.local_office.workspace import (
    KEYS,
    LocalOfficeBlocked,
    load_manifest,
    read_private,
    secret,
    workspace_lock,
)
from safe_agent.security.principal import AccessDenied, SignedOperatorTokens
from safe_agent.storage.models import FakeProviderRow

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "office"
    initialize(root, MIGRATIONS)
    return root


def test_init_uses_migrations_and_restart_does_not_reseed(workspace):
    value = load_manifest(workspace)
    assert value.migration_head == "0013_coordinated_planning"
    assert value.live_providers_enabled is False
    assert (workspace.stat().st_mode & 0o777) == 0o700
    for filename in (*KEYS, "office.sqlite", "READY", "workspace.json", "workspace.mac"):
        assert (workspace / filename).stat().st_mode & 0o777 == 0o600
    before = (workspace / "office.sqlite").read_bytes()
    first = open_office(workspace)
    snapshot = first.provider.snapshot("org_demo")
    first.close()
    second = open_office(workspace)
    assert second.provider.snapshot("org_demo") == snapshot
    second.close()
    assert (workspace / "office.sqlite").read_bytes() == before


def test_init_refuses_existing_workspace_without_altering_any_file(workspace):
    before = {p.name: p.read_bytes() for p in workspace.iterdir() if p.is_file()}
    with pytest.raises(FileExistsError):
        initialize(workspace, MIGRATIONS)
    assert before == {p.name: p.read_bytes() for p in workspace.iterdir() if p.is_file()}


def test_init_ignores_and_restores_inherited_database_url(tmp_path, monkeypatch):
    inherited = "postgresql+psycopg://invalid.invalid/never_connect"
    monkeypatch.setenv("SAFE_AGENT_DATABASE_URL", inherited)
    root = tmp_path / "owned"
    initialize(root, MIGRATIONS)
    assert os.environ["SAFE_AGENT_DATABASE_URL"] == inherited
    assert (root / "office.sqlite").is_file()


def test_missing_migrations_do_not_create_workspace(tmp_path):
    root = tmp_path / "missing"
    with pytest.raises(LocalOfficeBlocked):
        initialize(root, tmp_path / "no-migrations")
    assert not root.exists()


@pytest.mark.parametrize("name", ["READY", "workspace.mac", "workspace.json", "broker.key"])
def test_missing_workspace_evidence_is_not_regenerated(workspace, name):
    (workspace / name).unlink()
    with pytest.raises((OSError, ValueError)):
        open_office(workspace)
    assert not (workspace / name).exists()


def test_missing_database_is_not_recreated(workspace):
    (workspace / "office.sqlite").unlink()
    with pytest.raises(OSError):
        open_office(workspace)
    assert not (workspace / "office.sqlite").exists()


def test_missing_provider_row_is_not_reseeded(workspace):
    local = open_office(workspace)
    with local.repo.engine.begin() as connection:
        connection.execute(delete(FakeProviderRow))
    local.close()
    before = (workspace / "office.sqlite").read_bytes()
    with pytest.raises(LocalOfficeBlocked, match="PERSISTED_PROVIDER_REQUIRED"):
        open_office(workspace)
    assert (workspace / "office.sqlite").read_bytes() == before


@pytest.mark.parametrize("filename", KEYS)
def test_changed_key_is_rejected(workspace, filename):
    manifest = load_manifest(workspace)
    (workspace / filename).write_bytes(b"0" * 64)
    with pytest.raises(LocalOfficeBlocked, match="KEY_CHANGED"):
        secret(workspace, filename, manifest)


@pytest.mark.parametrize("filename", (*KEYS, "READY", "workspace.json", "workspace.mac"))
def test_public_private_files_are_rejected(workspace, filename):
    (workspace / filename).chmod(0o644)
    with pytest.raises(LocalOfficeBlocked):
        read_private(workspace / filename)


def test_public_directory_and_symlinked_workspace_are_rejected(workspace, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(LocalOfficeBlocked):
        load_manifest(link)
    workspace.chmod(0o755)
    with pytest.raises(LocalOfficeBlocked):
        load_manifest(workspace)


def test_symlink_and_hardlinked_secret_are_rejected(workspace, tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"1" * 64)
    target.chmod(0o600)
    path = workspace / "operator.key"
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(OSError):
        read_private(path)
    path.unlink()
    path.hardlink_to(target)
    with pytest.raises(LocalOfficeBlocked):
        read_private(path)


def test_cooperative_lock_is_exclusive_and_released(workspace):
    with workspace_lock(workspace):
        with pytest.raises(LocalOfficeBlocked, match="WORKSPACE_BUSY"):
            with workspace_lock(workspace):
                pytest.fail("competing operation acquired lock")
    with workspace_lock(workspace):
        pass


@pytest.mark.parametrize("role", ["requester", "approver"])
def test_sessions_use_fixed_distinct_scopes_and_expire(workspace, role):
    value = load_manifest(workspace)
    signer = SignedOperatorTokens(secret(workspace, "operator.key", value))
    token = issue_session(workspace, role, lifetime=60)
    principal = signer.verify(token, now=datetime.now(UTC))
    assert principal.actor_id == "local_" + role
    assert ("task:approve" in principal.scopes) == (role == "approver")
    assert ("task:create" in principal.scopes) == (role == "requester")
    assert all(not scope.startswith("connection:") for scope in principal.scopes)
    with pytest.raises(AccessDenied):
        signer.verify(token, now=principal.authenticated_until)


@pytest.mark.parametrize("lifetime", [0, 59, 3601])
def test_session_lifetime_bounds(lifetime):
    with pytest.raises(LocalOfficeBlocked):
        principal_for("requester", now=datetime.now(UTC), lifetime=lifetime)


def test_session_cannot_choose_new_identity():
    with pytest.raises(LocalOfficeBlocked):
        principal_for("administrator", now=datetime.now(UTC))


def test_tokens_from_another_workspace_fail(workspace, tmp_path):
    other = tmp_path / "other"
    initialize(other, MIGRATIONS)
    token = issue_session(workspace, "requester")
    signer = SignedOperatorTokens(secret(other, "operator.key", load_manifest(other)))
    with pytest.raises(AccessDenied):
        signer.verify(token, now=datetime.now(UTC))
