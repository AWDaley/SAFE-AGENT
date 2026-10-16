"""Explicit local maintenance: workspace upgrade/rollback and signed billing trust.

Normal serve/status/open paths never call these functions. Every mutation requires
all cooperative process locks and owner-only files. This is a local-development
safety boundary, not protection from the owning UID or root.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import Field

from safe_agent.billing.distribution import (
    distribution_root,
    verify_bundle,
    verify_installed_bundle,
)
from safe_agent.billing.keyring import TrustedBillingVerifierKeyring
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import Digest, StrictModel
from safe_agent.contracts.billing_distribution import (
    InstalledBillingKeyringState,
    PinnedBillingDistributionRoot,
    SignedBillingKeyringBundle,
)
from safe_agent.local_office.workspace import (
    HEAD,
    KEYS,
    LocalOfficeBlocked,
    WorkspaceManifest,
    absolute_path,
    load_manifest,
    private_directory,
    read_private,
    secret,
    workspace_lock,
    write_new,
)

LEGACY_HEAD: Final[Literal["0013_coordinated_planning"]] = "0013_coordinated_planning"
_STAGE_DB = ".office.sqlite.upgrade"
_ROLLBACK_DB = ".office.sqlite.rollback"
_JOURNAL = ".upgrade-journal.json"
_JOURNAL_MAC = ".upgrade-journal.mac"
_LAST_UPGRADE = "last-upgrade.json"
_LAST_UPGRADE_MAC = "last-upgrade.mac"
_ROOT = "billing-distribution-root.json"
_ROOT_MAC = "billing-distribution-root.mac"
_BUNDLE = "billing-keyring.bundle.json"
_STATE = "billing-keyring.state.json"
_STATE_MAC = "billing-keyring.state.mac"


class LegacyWorkspaceManifest(StrictModel):
    schema_version: Literal["safe_agent.local_workspace.v1"] = "safe_agent.local_workspace.v1"
    mode: Literal["PERSISTENT_SYNTHETIC_ONLY"] = "PERSISTENT_SYNTHETIC_ONLY"
    workspace_id: str = Field(pattern=r"^local_[0-9a-f]{32}$")
    migration_head: Literal["0013_coordinated_planning"] = LEGACY_HEAD
    created_at: datetime
    policy_hash: Digest
    seed_hash: Digest
    key_hashes: dict[str, str]
    live_providers_enabled: Literal[False] = False


class WorkspaceUpgradeBackup(StrictModel):
    schema_version: Literal["safe_agent.workspace_upgrade_backup.v1"] = (
        "safe_agent.workspace_upgrade_backup.v1"
    )
    backup_id: str = Field(pattern=r"^backup_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^local_[0-9a-f]{32}$")
    source_head: Literal["0013_coordinated_planning"] = LEGACY_HEAD
    target_head: Literal["0014_billing_verifier_identity"] = HEAD
    created_at: datetime
    database_sha256: Digest
    workspace_json_sha256: Digest
    workspace_mac_sha256: Digest
    ready_sha256: Digest
    key_hashes: dict[str, str]


class WorkspaceUpgradeJournal(StrictModel):
    schema_version: Literal["safe_agent.workspace_upgrade_journal.v1"] = (
        "safe_agent.workspace_upgrade_journal.v1"
    )
    workspace_id: str
    backup_id: str
    backup_path_hash: Digest
    source_head: Literal["0013_coordinated_planning"] = LEGACY_HEAD
    target_head: Literal["0014_billing_verifier_identity"] = HEAD
    source_database_sha256: Digest
    staged_database_sha256: Digest
    next_manifest_sha256: Digest
    started_at: datetime


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_file(path: Path, *, maximum: int = 1_073_741_824) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not os.path.isfile(path) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_size > maximum):
            raise LocalOfficeBlocked("WORKSPACE_FILE_DENIED")
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                return "sha256:" + digest.hexdigest()
            total += len(block)
            if total > maximum:
                raise LocalOfficeBlocked("WORKSPACE_FILE_DENIED")
            digest.update(block)
    finally:
        os.close(fd)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LocalOfficeBlocked("WORKSPACE_MANIFEST_DENIED")
        result[key] = value
    return result


def _raw_broker_key(root: Path, expected_hash: str | None = None) -> bytes:
    value = read_private(root / "broker.key", maximum=64)
    if re.fullmatch(rb"[0-9a-f]{64}", value) is None:
        raise LocalOfficeBlocked("WORKSPACE_SECRET_DENIED")
    if expected_hash is not None and not hmac.compare_digest(
        hashlib.sha256(value).hexdigest(), expected_hash,
    ):
        raise LocalOfficeBlocked("WORKSPACE_KEY_CHANGED")
    return value


def _validate_key_hashes(root: Path, hashes: dict[str, str]) -> None:
    if set(hashes) != set(KEYS):
        raise LocalOfficeBlocked("WORKSPACE_MANIFEST_DENIED")
    for name in KEYS:
        value = read_private(root / name, maximum=64)
        if (re.fullmatch(rb"[0-9a-f]{64}", value) is None
                or not hmac.compare_digest(
                    hashlib.sha256(value).hexdigest(), hashes[name],
                )):
            raise LocalOfficeBlocked("WORKSPACE_KEY_CHANGED")


def load_legacy_manifest(root: Path) -> LegacyWorkspaceManifest:
    root = private_directory(root)
    data = read_private(root / "workspace.json")
    json.loads(data, object_pairs_hook=_unique)
    try:
        value = LegacyWorkspaceManifest.model_validate_json(data)
    except Exception:
        raise LocalOfficeBlocked("WORKSPACE_LEGACY_MANIFEST_REQUIRED") from None
    if value.created_at.tzinfo is None:
        raise LocalOfficeBlocked("WORKSPACE_MANIFEST_DENIED")
    _validate_key_hashes(root, value.key_hashes)
    key = _raw_broker_key(root, value.key_hashes["broker.key"])
    expected = hmac.new(key, data, hashlib.sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(expected, read_private(root / "workspace.mac", maximum=64)):
        raise LocalOfficeBlocked("WORKSPACE_MANIFEST_DENIED")
    if read_private(root / "READY", maximum=128).decode("ascii") != sha256(value):
        raise LocalOfficeBlocked("WORKSPACE_INITIALIZATION_INCOMPLETE")
    private_directory(root / "inference")
    return value


def _database_head(path: Path, expected: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not os.path.isfile(path) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or (info.st_mode & 0o777) != 0o600):
            raise LocalOfficeBlocked("DATABASE_FILE_DENIED")
    finally:
        os.close(fd)
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            raise LocalOfficeBlocked("DATABASE_SIDE_FILE_PRESENT")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("SELECT version_num FROM alembic_version").fetchall() != [(expected,)]:
            raise LocalOfficeBlocked("DATABASE_MIGRATION_REQUIRED")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise LocalOfficeBlocked("DATABASE_INTEGRITY_DENIED")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise LocalOfficeBlocked("DATABASE_INTEGRITY_DENIED")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_private(path: Path, content: bytes) -> None:
    if path.exists():
        read_private(path, maximum=max(len(content), 1_048_576))
    temporary = path.with_name("." + path.name + ".next")
    if temporary.exists() or temporary.is_symlink():
        raise LocalOfficeBlocked("MAINTENANCE_RECOVERY_REQUIRED")
    write_new(temporary, content)
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _copy_exact(source: Path, destination: Path, *, maximum: int = 1_073_741_824) -> None:
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    destination_fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
    )
    try:
        source_info = os.fstat(source_fd)
        if (not os.path.isfile(source) or source_info.st_uid != os.getuid()
                or source_info.st_nlink != 1 or source_info.st_size > maximum):
            raise LocalOfficeBlocked("WORKSPACE_FILE_DENIED")
        total = 0
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > maximum or os.write(destination_fd, block) != len(block):
                raise LocalOfficeBlocked("WORKSPACE_FILE_DENIED")
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        os.close(destination_fd)


def _backup_database(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5) as src:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    os.chmod(destination, 0o600)


def _migration_config(migrations: Path) -> Config:
    migrations = absolute_path(migrations)
    if not (migrations / "env.py").is_file() or not (migrations / "versions").is_dir():
        raise LocalOfficeBlocked("MIGRATION_SOURCE_REQUIRED")
    config = Config()
    config.set_main_option("script_location", str(migrations).replace("%", "%%"))
    if ScriptDirectory.from_config(config).get_heads() != [HEAD]:
        raise LocalOfficeBlocked("MIGRATION_SOURCE_MISMATCH")
    return config


def _upgrade_database(path: Path, migrations: Path) -> None:
    config = _migration_config(migrations)
    old_url = os.environ.get("SAFE_AGENT_DATABASE_URL")
    os.environ["SAFE_AGENT_DATABASE_URL"] = "sqlite+pysqlite:///" + str(path)
    try:
        command.upgrade(config, HEAD)
    finally:
        if old_url is None:
            os.environ.pop("SAFE_AGENT_DATABASE_URL", None)
        else:
            os.environ["SAFE_AGENT_DATABASE_URL"] = old_url
    _database_head(path, HEAD)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        names = {
            row[1] for row in db.execute(
                "PRAGMA table_info(safe_inference_billing_attestations)"
            )
        }
    if not {
        "verification_scheme", "verifier_key_id",
        "verifier_public_key_hash", "source_adapter_id",
    } <= names:
        raise LocalOfficeBlocked("DATABASE_MIGRATION_REQUIRED")


def _all_process_locks(root: Path) -> ExitStack:
    stack = ExitStack()
    try:
        stack.enter_context(workspace_lock(root, "api.lock"))
        stack.enter_context(workspace_lock(root, "worker.lock"))
        stack.enter_context(workspace_lock(root))
        return stack
    except BaseException:
        stack.close()
        raise


def _create_backup(
    root: Path, backup: Path, manifest: LegacyWorkspaceManifest, *, now: datetime,
) -> WorkspaceUpgradeBackup:
    backup = absolute_path(backup)
    if backup == root or root in backup.parents or backup in root.parents:
        raise LocalOfficeBlocked("BACKUP_PATH_DENIED")
    if not backup.parent.is_dir():
        raise LocalOfficeBlocked("BACKUP_PARENT_MISSING")
    backup.mkdir(mode=0o700, exist_ok=False)
    _backup_database(root / "office.sqlite", backup / "office.sqlite")
    raw_json = read_private(root / "workspace.json")
    raw_mac = read_private(root / "workspace.mac", maximum=64)
    raw_ready = read_private(root / "READY", maximum=128)
    write_new(backup / "workspace.json", raw_json)
    write_new(backup / "workspace.mac", raw_mac)
    write_new(backup / "READY", raw_ready)
    record = WorkspaceUpgradeBackup(
        backup_id="backup_" + os.urandom(16).hex(),
        workspace_id=manifest.workspace_id,
        created_at=now,
        database_sha256=_digest_file(backup / "office.sqlite"),
        workspace_json_sha256=_digest_bytes(raw_json),
        workspace_mac_sha256=_digest_bytes(raw_mac),
        ready_sha256=_digest_bytes(raw_ready),
        key_hashes=dict(manifest.key_hashes),
    )
    data = canonical_json(record)
    key = _raw_broker_key(root, manifest.key_hashes["broker.key"])
    write_new(backup / "backup.json", data)
    write_new(
        backup / "backup.mac",
        hmac.new(key, data, hashlib.sha256).hexdigest().encode("ascii"),
    )
    _fsync_dir(backup)
    return record


def _verify_backup(root: Path, backup: Path) -> WorkspaceUpgradeBackup:
    backup = private_directory(absolute_path(backup))
    data = read_private(backup / "backup.json")
    try:
        record = WorkspaceUpgradeBackup.model_validate_json(data)
    except Exception:
        raise LocalOfficeBlocked("BACKUP_EVIDENCE_DENIED") from None
    key = _raw_broker_key(root, record.key_hashes.get("broker.key"))
    expected = hmac.new(key, data, hashlib.sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(expected, read_private(backup / "backup.mac", maximum=64)):
        raise LocalOfficeBlocked("BACKUP_EVIDENCE_DENIED")
    _validate_key_hashes(root, record.key_hashes)
    checks = (
        (_digest_file(backup / "office.sqlite"), record.database_sha256),
        (_digest_bytes(read_private(backup / "workspace.json")), record.workspace_json_sha256),
        (_digest_bytes(read_private(backup / "workspace.mac", maximum=64)), record.workspace_mac_sha256),
        (_digest_bytes(read_private(backup / "READY", maximum=128)), record.ready_sha256),
    )
    if any(not hmac.compare_digest(actual, expected) for actual, expected in checks):
        raise LocalOfficeBlocked("BACKUP_EVIDENCE_DENIED")
    _database_head(backup / "office.sqlite", LEGACY_HEAD)
    return record


def upgrade_workspace(
    root: Path, migrations: Path, backup: Path, *, now: datetime | None = None,
) -> dict[str, object]:
    root, backup = absolute_path(root), absolute_path(backup)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    with _all_process_locks(root):
        if (root / _JOURNAL).exists() or (root / _JOURNAL_MAC).exists():
            raise LocalOfficeBlocked("UPGRADE_ROLLBACK_REQUIRED")
        try:
            load_manifest(root)
        except Exception:
            legacy = load_legacy_manifest(root)
        else:
            raise LocalOfficeBlocked("WORKSPACE_ALREADY_CURRENT")
        _database_head(root / "office.sqlite", LEGACY_HEAD)
        _migration_config(migrations)
        record = _create_backup(root, backup, legacy, now=current_time)

        stage = root / _STAGE_DB
        if stage.exists() or stage.is_symlink():
            raise LocalOfficeBlocked("MAINTENANCE_RECOVERY_REQUIRED")
        _backup_database(root / "office.sqlite", stage)
        _upgrade_database(stage, migrations)

        next_manifest = WorkspaceManifest(
            workspace_id=legacy.workspace_id,
            created_at=legacy.created_at,
            policy_hash=legacy.policy_hash,
            seed_hash=legacy.seed_hash,
            key_hashes=dict(legacy.key_hashes),
        )
        manifest_data = canonical_json(next_manifest)
        broker_key = _raw_broker_key(root, legacy.key_hashes["broker.key"])
        manifest_mac = hmac.new(
            broker_key, manifest_data, hashlib.sha256,
        ).hexdigest().encode("ascii")
        ready = sha256(next_manifest).encode("ascii")

        for name, data in (
            (".workspace.json.upgrade", manifest_data),
            (".workspace.mac.upgrade", manifest_mac),
            (".READY.upgrade", ready),
        ):
            if (root / name).exists() or (root / name).is_symlink():
                raise LocalOfficeBlocked("MAINTENANCE_RECOVERY_REQUIRED")
            write_new(root / name, data)

        journal = WorkspaceUpgradeJournal(
            workspace_id=legacy.workspace_id,
            backup_id=record.backup_id,
            backup_path_hash=sha256({"backup": str(backup)}),
            source_database_sha256=record.database_sha256,
            staged_database_sha256=_digest_file(stage),
            next_manifest_sha256=_digest_bytes(manifest_data),
            started_at=current_time,
        )
        journal_data = canonical_json(journal)
        write_new(root / _JOURNAL, journal_data)
        write_new(
            root / _JOURNAL_MAC,
            hmac.new(broker_key, journal_data, hashlib.sha256).hexdigest().encode("ascii"),
        )
        _fsync_dir(root)

        os.replace(stage, root / "office.sqlite")
        _fsync_dir(root)
        for temporary, target in (
            (".workspace.json.upgrade", "workspace.json"),
            (".workspace.mac.upgrade", "workspace.mac"),
            (".READY.upgrade", "READY"),
        ):
            os.replace(root / temporary, root / target)
            _fsync_dir(root)

        latest = load_manifest(root)
        if latest.workspace_id != legacy.workspace_id:
            raise LocalOfficeBlocked("UPGRADE_VERIFICATION_FAILED")
        _database_head(root / "office.sqlite", HEAD)

        journal_mac = read_private(root / _JOURNAL_MAC, maximum=64)
        _atomic_private(root / _LAST_UPGRADE, journal_data)
        _atomic_private(root / _LAST_UPGRADE_MAC, journal_mac)
        (root / _JOURNAL).unlink()
        (root / _JOURNAL_MAC).unlink()
        _fsync_dir(root)
        return {
            "status": "UPGRADED",
            "workspace_id": latest.workspace_id,
            "source_head": LEGACY_HEAD,
            "target_head": HEAD,
            "backup_id": record.backup_id,
            "live_providers_enabled": False,
            "production_authorized": False,
        }


def rollback_workspace(
    root: Path, backup: Path, *, now: datetime | None = None,
) -> dict[str, object]:
    del now
    root, backup = absolute_path(root), absolute_path(backup)
    with _all_process_locks(root):
        record = _verify_backup(root, backup)
        restore = root / _ROLLBACK_DB
        if restore.exists() or restore.is_symlink():
            raise LocalOfficeBlocked("MAINTENANCE_RECOVERY_REQUIRED")
        _copy_exact(backup / "office.sqlite", restore)
        _database_head(restore, LEGACY_HEAD)
        for name in ("workspace.json", "workspace.mac", "READY"):
            temporary = root / ("." + name + ".rollback")
            if temporary.exists() or temporary.is_symlink():
                raise LocalOfficeBlocked("MAINTENANCE_RECOVERY_REQUIRED")
            _copy_exact(backup / name, temporary, maximum=1_048_576)

        os.replace(restore, root / "office.sqlite")
        _fsync_dir(root)
        for name in ("workspace.json", "workspace.mac", "READY"):
            os.replace(root / ("." + name + ".rollback"), root / name)
            _fsync_dir(root)
        legacy = load_legacy_manifest(root)
        _database_head(root / "office.sqlite", LEGACY_HEAD)
        for name in (_JOURNAL, _JOURNAL_MAC):
            try:
                (root / name).unlink()
            except FileNotFoundError:
                pass
        _fsync_dir(root)
        return {
            "status": "ROLLED_BACK",
            "workspace_id": legacy.workspace_id,
            "migration_head": LEGACY_HEAD,
            "backup_id": record.backup_id,
            "requires_reviewed_upgrade_before_current_runtime": True,
            "live_providers_enabled": False,
            "production_authorized": False,
        }


def _hmac_file(root: Path, filename: str, value: bytes) -> None:
    key = secret(root, "broker.key", load_manifest(root))
    _atomic_private(
        root / filename,
        hmac.new(key, value, hashlib.sha256).hexdigest().encode("ascii"),
    )


def _verified_hmac_json(root: Path, filename: str, mac_filename: str, model):
    data = read_private(root / filename, maximum=65_536)
    key = secret(root, "broker.key", load_manifest(root))
    expected = hmac.new(key, data, hashlib.sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(expected, read_private(root / mac_filename, maximum=64)):
        raise LocalOfficeBlocked("BILLING_TRUST_STORE_DENIED")
    try:
        return model.model_validate_json(data)
    except Exception:
        raise LocalOfficeBlocked("BILLING_TRUST_STORE_DENIED") from None


def pin_billing_distribution_root(
    root: Path, *, distribution_key_id: str,
    public_key_file: Path, now: datetime | None = None,
) -> PinnedBillingDistributionRoot:
    root, public_key_file = absolute_path(root), absolute_path(public_key_file)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    with _all_process_locks(root):
        load_manifest(root)
        raw = read_private(public_key_file, maximum=64)
        if re.fullmatch(rb"[0-9a-f]{64}", raw) is None:
            raise LocalOfficeBlocked("BILLING_ROOT_KEY_DENIED")
        pinned = distribution_root(
            distribution_key_id=distribution_key_id,
            public_key=bytes.fromhex(raw.decode("ascii")),
            pinned_at=current_time,
        )
        data = canonical_json(pinned)
        if (root / _ROOT).exists() or (root / _ROOT_MAC).exists():
            existing = _verified_hmac_json(
                root, _ROOT, _ROOT_MAC, PinnedBillingDistributionRoot,
            )
            if existing != pinned.model_copy(update={"pinned_at": existing.pinned_at}):
                raise LocalOfficeBlocked("BILLING_ROOT_ALREADY_PINNED")
            return existing
        write_new(root / _ROOT, data)
        key = secret(root, "broker.key", load_manifest(root))
        write_new(
            root / _ROOT_MAC,
            hmac.new(key, data, hashlib.sha256).hexdigest().encode("ascii"),
        )
        _fsync_dir(root)
        return pinned


def install_billing_keyring(
    root: Path, bundle_file: Path, *, now: datetime | None = None,
) -> InstalledBillingKeyringState:
    root, bundle_file = absolute_path(root), absolute_path(bundle_file)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    with _all_process_locks(root):
        load_manifest(root)
        pinned = _verified_hmac_json(
            root, _ROOT, _ROOT_MAC, PinnedBillingDistributionRoot,
        )
        raw = read_private(bundle_file, maximum=65_536)
        try:
            bundle = SignedBillingKeyringBundle.model_validate_json(raw)
        except Exception:
            raise LocalOfficeBlocked("BILLING_KEYRING_BUNDLE_DENIED") from None

        state: InstalledBillingKeyringState | None = None
        state_exists = (root / _STATE).exists() or (root / _STATE_MAC).exists()
        bundle_exists = (root / _BUNDLE).exists()
        if state_exists != bundle_exists:
            raise LocalOfficeBlocked("BILLING_TRUST_STORE_DENIED")
        if state_exists:
            state = _verified_hmac_json(
                root, _STATE, _STATE_MAC, InstalledBillingKeyringState,
            )
            installed = SignedBillingKeyringBundle.model_validate_json(
                read_private(root / _BUNDLE, maximum=65_536)
            )
            verify_installed_bundle(
                installed, root=pinned, state=state, now=current_time,
            )

        try:
            checked, digest = verify_bundle(
                bundle, root=pinned, now=current_time, current=state,
            )
        except Exception:
            raise LocalOfficeBlocked("BILLING_KEYRING_BUNDLE_DENIED") from None
        next_state = InstalledBillingKeyringState(
            distribution_key_id=checked.distribution_key_id,
            generation=checked.generation,
            bundle_digest=digest,
            installed_at=current_time,
        )
        bundle_data = canonical_json(checked)
        state_data = canonical_json(next_state)
        if bundle_exists:
            _atomic_private(root / _BUNDLE, bundle_data)
            _atomic_private(root / _STATE, state_data)
            _hmac_file(root, _STATE_MAC, state_data)
        else:
            write_new(root / _BUNDLE, bundle_data)
            write_new(root / _STATE, state_data)
            key = secret(root, "broker.key", load_manifest(root))
            write_new(
                root / _STATE_MAC,
                hmac.new(key, state_data, hashlib.sha256).hexdigest().encode("ascii"),
            )
            _fsync_dir(root)
        return next_state


def load_installed_billing_keyring(
    root: Path, *, now: datetime | None = None,
) -> TrustedBillingVerifierKeyring:
    root = absolute_path(root)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    load_manifest(root)
    pinned = _verified_hmac_json(root, _ROOT, _ROOT_MAC, PinnedBillingDistributionRoot)
    state = _verified_hmac_json(root, _STATE, _STATE_MAC, InstalledBillingKeyringState)
    try:
        bundle = SignedBillingKeyringBundle.model_validate_json(
            read_private(root / _BUNDLE, maximum=65_536)
        )
        checked, _ = verify_installed_bundle(
            bundle, root=pinned, state=state, now=current_time,
        )
        return TrustedBillingVerifierKeyring(checked.keyring)
    except Exception:
        raise LocalOfficeBlocked("BILLING_TRUST_STORE_DENIED") from None
