"""Owner-only, fail-closed local workspace files and cooperative process locks.

These checks prevent supported misconfiguration, not attacks by the owning UID
or root. The local launcher is explicitly not a multi-user production sandbox.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Iterator, Literal

from alembic.script import ScriptDirectory
from pydantic import Field

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.base import StrictModel

HEAD: Final = "0014_billing_verifier_identity"
BENCHMARK_HEAD: Final = "0016_episode_inference"
KEYS = ("broker.key", "operator.key", "workload.token", "dispatch.key")
LOCKS = ("operation.lock", "api.lock", "worker.lock")


class LocalOfficeBlocked(ValueError):
    """Only fixed codes from trusted code may be reported to a local operator."""


def require_office_migration_source(script: ScriptDirectory) -> None:
    """Keep office workspaces at their accepted revision despite additive tables.

    Only the explicitly reviewed benchmark tail is accepted. This is not a
    general permission to migrate an office workspace to arbitrary descendants.
    """
    heads = script.get_heads()
    if heads == [HEAD]:
        return
    if heads != [BENCHMARK_HEAD]:
        raise LocalOfficeBlocked("MIGRATION_SOURCE_MISMATCH")
    for revision, parent in ((BENCHMARK_HEAD, "0015_benchmark_episodes"),
                              ("0015_benchmark_episodes", HEAD)):
        node = script.get_revision(revision)
        if node is None or node.down_revision != parent:
            raise LocalOfficeBlocked("MIGRATION_SOURCE_MISMATCH")


class WorkspaceManifest(StrictModel):
    schema_version: Literal["safe_agent.local_workspace.v1"] = "safe_agent.local_workspace.v1"
    mode: Literal["PERSISTENT_SYNTHETIC_ONLY"] = "PERSISTENT_SYNTHETIC_ONLY"
    workspace_id: str = Field(pattern=r"^local_[0-9a-f]{32}$")
    migration_head: Literal["0014_billing_verifier_identity"] = HEAD
    created_at: datetime
    policy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    seed_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    key_hashes: dict[str, str]
    live_providers_enabled: Literal[False] = False


def absolute_path(value: Path) -> Path:
    # Reject traversal and every symlink component rather than resolving it away.
    value = value.expanduser()
    if ".." in value.parts:
        raise LocalOfficeBlocked("WORKSPACE_PATH_DENIED")
    path = value.absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise LocalOfficeBlocked("WORKSPACE_SYMLINK_DENIED")
    return path


def private_directory(path: Path) -> Path:
    path = absolute_path(path)
    try:
        info = path.stat()
    except OSError:
        raise LocalOfficeBlocked("WORKSPACE_MISSING") from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise LocalOfficeBlocked("WORKSPACE_PERMISSIONS_DENIED")
    return path


def read_private(path: Path, *, maximum: int = 16384) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > maximum):
            raise LocalOfficeBlocked("WORKSPACE_FILE_DENIED")
        result = bytearray()
        while len(result) <= maximum:
            block = os.read(fd, min(4096, maximum + 1 - len(result)))
            if not block:
                return bytes(result)
            result.extend(block)
        raise LocalOfficeBlocked("WORKSPACE_FILE_DENIED")
    finally:
        os.close(fd)


def write_new(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LocalOfficeBlocked("WORKSPACE_MANIFEST_DENIED")
        result[key] = value
    return result


def secret(root: Path, filename: str, manifest: WorkspaceManifest | None = None) -> bytes:
    if filename not in KEYS:
        raise LocalOfficeBlocked("WORKSPACE_SECRET_DENIED")
    value = read_private(root / filename, maximum=64)
    if re.fullmatch(rb"[0-9a-f]{64}", value) is None:
        raise LocalOfficeBlocked("WORKSPACE_SECRET_DENIED")
    if manifest is not None and not hmac.compare_digest(
        hashlib.sha256(value).hexdigest(), manifest.key_hashes.get(filename, ""),
    ):
        raise LocalOfficeBlocked("WORKSPACE_KEY_CHANGED")
    return value


def load_manifest(root: Path) -> WorkspaceManifest:
    root = private_directory(root)
    data = read_private(root / "workspace.json")
    json.loads(data, object_pairs_hook=_unique)
    value = WorkspaceManifest.model_validate_json(data)
    if (value.created_at.tzinfo is None or set(value.key_hashes) != set(KEYS)
            or any(re.fullmatch(r"[0-9a-f]{64}", v) is None for v in value.key_hashes.values())):
        raise LocalOfficeBlocked("WORKSPACE_MANIFEST_DENIED")
    key = secret(root, "broker.key", value)
    expected = hmac.new(key, data, hashlib.sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(expected, read_private(root / "workspace.mac", maximum=64)):
        raise LocalOfficeBlocked("WORKSPACE_MANIFEST_DENIED")
    if read_private(root / "READY", maximum=128).decode("ascii") != sha256(value):
        raise LocalOfficeBlocked("WORKSPACE_INITIALIZATION_INCOMPLETE")
    private_directory(root / "inference")
    return value


def create_layout(root: Path, *, policy_hash: str, seed_hash: str) -> WorkspaceManifest:
    """Create only a new directory. Failure never deletes or repairs existing data."""
    root = absolute_path(root)
    if not root.parent.is_dir():
        raise LocalOfficeBlocked("WORKSPACE_PARENT_MISSING")
    root.mkdir(mode=0o700, exist_ok=False)
    hashes: dict[str, str] = {}
    for name in KEYS:
        value = secrets.token_hex(32).encode("ascii")
        write_new(root / name, value)
        hashes[name] = hashlib.sha256(value).hexdigest()
    for name in LOCKS:
        write_new(root / name, b"")
    (root / "inference").mkdir(mode=0o700)
    return WorkspaceManifest(
        workspace_id="local_" + secrets.token_hex(16), created_at=datetime.now(UTC),
        policy_hash=policy_hash, seed_hash=seed_hash, key_hashes=hashes,
    )


def finish_layout(root: Path, value: WorkspaceManifest) -> None:
    data = canonical_json(value)
    write_new(root / "workspace.json", data)
    write_new(root / "workspace.mac", hmac.new(
        secret(root, "broker.key", value), data, hashlib.sha256,
    ).hexdigest().encode("ascii"))
    write_new(root / "READY", sha256(value).encode("ascii"))
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def workspace_lock(root: Path, name: str = "operation.lock") -> Iterator[None]:
    if name not in LOCKS:
        raise LocalOfficeBlocked("WORKSPACE_LOCK_DENIED")
    private_directory(root)
    path = root / name
    # The file must already exist; serve/status never repairs a broken workspace.
    read_private(path, maximum=0)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise LocalOfficeBlocked("WORKSPACE_LOCK_DENIED")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LocalOfficeBlocked("WORKSPACE_BUSY") from None
        yield
    finally:
        os.close(fd)
