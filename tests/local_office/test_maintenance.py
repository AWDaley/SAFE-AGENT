"""Explicit workspace maintenance and signed billing-keyring distribution."""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine

from safe_agent.billing.distribution import OfflineBillingKeyringDistributor
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.billing_verification import (
    BillingVerifierKeyringConfig,
    BillingVerifierPublicKey,
)
from safe_agent.local_office.maintenance import (
    LEGACY_HEAD,
    LegacyWorkspaceManifest,
    install_billing_keyring,
    load_installed_billing_keyring,
    load_legacy_manifest,
    pin_billing_distribution_root,
    rollback_workspace,
    upgrade_workspace,
)
from safe_agent.local_office.state import (
    initialize,
    local_policy,
    open_office,
    seed_source,
)
from safe_agent.local_office.workspace import (
    HEAD,
    LocalOfficeBlocked,
    create_layout,
    load_manifest,
    read_private,
    workspace_lock,
    write_new,
)
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.storage.repository import LedgerRepository
from tests.m2.conftest import NOW

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _migration(path: Path, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    old = os.environ.get("SAFE_AGENT_DATABASE_URL")
    os.environ["SAFE_AGENT_DATABASE_URL"] = "sqlite+pysqlite:///" + str(path)
    try:
        command.upgrade(config, revision)
    finally:
        if old is None:
            os.environ.pop("SAFE_AGENT_DATABASE_URL", None)
        else:
            os.environ["SAFE_AGENT_DATABASE_URL"] = old


def legacy_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "legacy-office"
    source = seed_source()
    latest = create_layout(
        root, policy_hash=sha256(local_policy()), seed_hash=sha256(source),
    )
    write_new(root / "office.sqlite", b"")
    _migration(root / "office.sqlite", LEGACY_HEAD)
    engine = create_engine("sqlite+pysqlite:///" + str(root / "office.sqlite"))
    repo = LedgerRepository(engine)
    try:
        DurableFakeProvider(repo.engine, source)
    finally:
        repo.close()
    legacy = LegacyWorkspaceManifest(
        workspace_id=latest.workspace_id,
        created_at=latest.created_at,
        policy_hash=latest.policy_hash,
        seed_hash=latest.seed_hash,
        key_hashes=dict(latest.key_hashes),
    )
    data = canonical_json(legacy)
    broker = read_private(root / "broker.key", maximum=64)
    write_new(root / "workspace.json", data)
    write_new(
        root / "workspace.mac",
        hmac.new(broker, data, hashlib.sha256).hexdigest().encode("ascii"),
    )
    write_new(root / "READY", sha256(legacy).encode("ascii"))
    assert load_legacy_manifest(root).migration_head == LEGACY_HEAD
    return root


def db_head(root: Path) -> str:
    with sqlite3.connect(root / "office.sqlite") as db:
        return db.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def _private_file(path: Path, data: bytes) -> Path:
    write_new(path, data)
    return path


def _public(private: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )


def _keyring(private: bytes, key_id: str) -> BillingVerifierKeyringConfig:
    return BillingVerifierKeyringConfig(
        issuer_id="billing_verifier_maintenance_test",
        keys=(BillingVerifierPublicKey(
            issuer_id="billing_verifier_maintenance_test",
            verifier_key_id=key_id,
            public_key_hex=_public(private).hex(),
            signing_not_before=NOW - timedelta(minutes=1),
            signing_not_after=NOW + timedelta(days=30),
            accept_until=NOW + timedelta(days=37),
        ),),
    )


def test_explicit_upgrade_creates_verified_backup_and_normal_start_never_auto_migrates(tmp_path):
    root = legacy_workspace(tmp_path)
    backup = tmp_path / "backup-before-0014"
    before_keys = {
        name: read_private(root / name, maximum=64)
        for name in ("broker.key", "operator.key", "workload.token", "dispatch.key")
    }
    before_provider = None
    with sqlite3.connect(root / "office.sqlite") as db:
        before_provider = db.execute(
            "SELECT state_json FROM safe_fake_provider_state"
        ).fetchone()[0]

    with pytest.raises((ValueError, OSError)):
        open_office(root)
    assert db_head(root) == LEGACY_HEAD
    result = upgrade_workspace(root, MIGRATIONS, backup, now=NOW)
    assert result["status"] == "UPGRADED"
    assert result["source_head"] == LEGACY_HEAD
    assert result["target_head"] == HEAD
    assert db_head(root) == HEAD
    assert load_manifest(root).migration_head == HEAD
    assert (backup.stat().st_mode & 0o777) == 0o700
    assert db_head(backup) == LEGACY_HEAD
    with sqlite3.connect(root / "office.sqlite") as db:
        assert db.execute("SELECT state_json FROM safe_fake_provider_state").fetchone()[0] == (
            before_provider
        )
        columns = {
            row[1] for row in db.execute(
                "PRAGMA table_info(safe_inference_billing_attestations)"
            )
        }
        assert {
            "verification_scheme", "verifier_key_id",
            "verifier_public_key_hash", "source_adapter_id",
        } <= columns
    for name, value in before_keys.items():
        assert read_private(root / name, maximum=64) == value
    local = open_office(root)
    local.close()
    with pytest.raises(LocalOfficeBlocked, match="ALREADY_CURRENT"):
        upgrade_workspace(root, MIGRATIONS, tmp_path / "unused-backup", now=NOW)


def test_verified_rollback_restores_legacy_state_and_can_be_reupgraded(tmp_path):
    root = legacy_workspace(tmp_path)
    backup = tmp_path / "backup-one"
    upgrade_workspace(root, MIGRATIONS, backup, now=NOW)
    assert db_head(root) == HEAD

    result = rollback_workspace(root, backup, now=NOW + timedelta(minutes=1))
    assert result["status"] == "ROLLED_BACK"
    assert result["migration_head"] == LEGACY_HEAD
    assert result["requires_reviewed_upgrade_before_current_runtime"] is True
    assert db_head(root) == LEGACY_HEAD
    assert load_legacy_manifest(root).migration_head == LEGACY_HEAD
    with pytest.raises((ValueError, OSError)):
        open_office(root)

    second = tmp_path / "backup-two"
    upgraded = upgrade_workspace(root, MIGRATIONS, second, now=NOW + timedelta(minutes=2))
    assert upgraded["status"] == "UPGRADED"
    assert db_head(root) == HEAD


def test_upgrade_refuses_active_process_and_bad_migration_source_without_mutation(tmp_path):
    root = legacy_workspace(tmp_path)
    before = (root / "office.sqlite").read_bytes()
    with workspace_lock(root, "api.lock"):
        with pytest.raises(LocalOfficeBlocked, match="WORKSPACE_BUSY"):
            upgrade_workspace(root, MIGRATIONS, tmp_path / "busy-backup", now=NOW)
    assert (root / "office.sqlite").read_bytes() == before
    assert db_head(root) == LEGACY_HEAD

    fake = tmp_path / "fake-migrations"
    fake.mkdir()
    with pytest.raises(LocalOfficeBlocked, match="MIGRATION_SOURCE_REQUIRED"):
        upgrade_workspace(root, fake, tmp_path / "bad-source-backup", now=NOW)
    assert db_head(root) == LEGACY_HEAD
    assert not (tmp_path / "bad-source-backup").exists()


def test_tampered_backup_cannot_restore(tmp_path):
    root = legacy_workspace(tmp_path)
    backup = tmp_path / "backup"
    upgrade_workspace(root, MIGRATIONS, backup, now=NOW)
    database = backup / "office.sqlite"
    data = bytearray(database.read_bytes())
    data[min(100, len(data) - 1)] ^= 1
    database.write_bytes(bytes(data))
    database.chmod(0o600)
    with pytest.raises(LocalOfficeBlocked, match="BACKUP_EVIDENCE_DENIED"):
        rollback_workspace(root, backup, now=NOW + timedelta(minutes=1))
    assert db_head(root) == HEAD


def test_signed_keyring_install_is_chained_and_old_bundle_cannot_roll_back(tmp_path):
    root = tmp_path / "current"
    initialize(root, MIGRATIONS)
    distribution_private = bytes(range(1, 33))
    root_file = _private_file(
        tmp_path / "distribution.pub", _public(distribution_private).hex().encode("ascii"),
    )
    pinned = pin_billing_distribution_root(
        root, distribution_key_id="billing_distribution_test_v1",
        public_key_file=root_file, now=NOW,
    )
    assert pinned.distribution_key_id == "billing_distribution_test_v1"

    distributor = OfflineBillingKeyringDistributor(
        distribution_key_id=pinned.distribution_key_id,
        private_key=distribution_private,
    )
    verifier_one = bytes(range(33, 65))
    first = distributor.sign(
        _keyring(verifier_one, "billing_verifier_key_v1"),
        generation=1, previous_bundle_digest=None,
        issued_at=NOW, expires_at=NOW + timedelta(days=20),
    )
    first_file = _private_file(
        tmp_path / "keyring-one.json", canonical_json(first),
    )
    state_one = install_billing_keyring(root, first_file, now=NOW + timedelta(minutes=1))
    assert state_one.generation == 1
    assert load_installed_billing_keyring(
        root, now=NOW + timedelta(minutes=2),
    ).issuer_id == "billing_verifier_maintenance_test"

    verifier_two = bytes(range(65, 97))
    second = distributor.sign(
        _keyring(verifier_two, "billing_verifier_key_v2"),
        generation=2, previous_bundle_digest=state_one.bundle_digest,
        issued_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(days=20),
    )
    second_file = _private_file(
        tmp_path / "keyring-two.json", canonical_json(second),
    )
    state_two = install_billing_keyring(root, second_file, now=NOW + timedelta(minutes=3))
    assert state_two.generation == 2
    assert state_two.bundle_digest != state_one.bundle_digest

    with pytest.raises(LocalOfficeBlocked, match="BUNDLE_DENIED"):
        install_billing_keyring(root, first_file, now=NOW + timedelta(minutes=4))
    assert load_installed_billing_keyring(
        root, now=NOW + timedelta(minutes=4),
    ).issuer_id == "billing_verifier_maintenance_test"

    for name in (
        "billing-distribution-root.json", "billing-distribution-root.mac",
        "billing-keyring.bundle.json", "billing-keyring.state.json",
        "billing-keyring.state.mac",
    ):
        assert (root / name).stat().st_mode & 0o777 == 0o600


def test_distribution_root_is_pinned_once_and_bundle_chain_is_strict(tmp_path):
    root = tmp_path / "current"
    initialize(root, MIGRATIONS)
    private = bytes(range(1, 33))
    public = _private_file(tmp_path / "root.pub", _public(private).hex().encode("ascii"))
    pin_billing_distribution_root(
        root, distribution_key_id="billing_distribution_test_v1",
        public_key_file=public, now=NOW,
    )
    other_private = bytes(range(97, 129))
    other = _private_file(
        tmp_path / "other.pub", _public(other_private).hex().encode("ascii"),
    )
    with pytest.raises(LocalOfficeBlocked, match="ALREADY_PINNED"):
        pin_billing_distribution_root(
            root, distribution_key_id="billing_distribution_test_v2",
            public_key_file=other, now=NOW + timedelta(minutes=1),
        )

    distributor = OfflineBillingKeyringDistributor(
        distribution_key_id="billing_distribution_test_v1",
        private_key=private,
    )
    forged_chain = distributor.sign(
        _keyring(bytes(range(33, 65)), "billing_verifier_key_v1"),
        generation=2,
        previous_bundle_digest="sha256:" + "0" * 64,
        issued_at=NOW, expires_at=NOW + timedelta(days=10),
    )
    path = _private_file(tmp_path / "bad-chain.json", canonical_json(forged_chain))
    with pytest.raises(LocalOfficeBlocked, match="BUNDLE_DENIED"):
        install_billing_keyring(root, path, now=NOW + timedelta(minutes=1))
