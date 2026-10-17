"""Persistent SQLite setup for a private, explicitly synthetic local workspace.

Initialization alone runs the existing Alembic migrations. Every later startup
requires the saved schema and provider state; it never creates or reseeds them.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import NullPool

from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.broker import ReferenceBroker
from safe_agent.canonical import sha256
from safe_agent.contracts.inference import InferenceRoutePolicy
from safe_agent.contracts.inference_admission import InferenceAdmissionPolicy
from safe_agent.contracts.inference_budget import InferenceBudgetLimits
from safe_agent.local_office.workspace import (
    HEAD,
    LocalOfficeBlocked,
    WorkspaceManifest,
    absolute_path,
    create_layout,
    finish_layout,
    load_manifest,
    secret,
    write_new,
)
from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.storage.models import FakeProviderRow
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.storage.repository import LedgerRepository
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.virtual_office import SyntheticOfficeState, VirtualOffice

HOST = "inference.example.com"
ROUTE = "local_office_route"
MODEL = "offline_v1"
ORGANIZATION = "org_demo"


def local_policy() -> InferenceAdmissionPolicy:
    # Nonzero conservative synthetic accounting is retained, including uncertain
    # reservations and the existing four-outstanding-attempt tenant limit.
    return InferenceAdmissionPolicy(
        route=InferenceRoutePolicy(route_id=ROUTE, model_revision=MODEL,
            max_estimated_cost_microusd=1000, output_microusd_per_million_tokens=100000),
        budget=InferenceBudgetLimits(global_microusd=10000, tenant_microusd=5000,
                                     task_microusd=2000, max_call_microusd=1000),
    )


def seed_source() -> SyntheticOfficeState:
    # Explicit built-in synthetic input only. There is no file-upload or live
    # connector configuration and no fallback from missing real customer data.
    raw = files("safe_agent.demo").joinpath("fixtures/office_state.json").read_text("utf-8")
    return SyntheticOfficeState.model_validate_json(raw)


def check_database(root: Path) -> None:
    path = root / "office.sqlite"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1):
            raise LocalOfficeBlocked("DATABASE_FILE_DENIED")
    finally:
        os.close(fd)
    # mode=ro cannot create an absent DB. No user-supplied connection URL.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("SELECT version_num FROM alembic_version").fetchall() != [(HEAD,)]:
            raise LocalOfficeBlocked("DATABASE_MIGRATION_REQUIRED")


def repository(root: Path) -> LedgerRepository:
    check_database(root)
    engine = create_engine("sqlite+pysqlite:///" + str(root / "office.sqlite"),
        connect_args={"check_same_thread": False, "timeout": 5}, poolclass=NullPool)
    @event.listens_for(engine, "connect")
    def foreign_keys(connection: Any, _: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
    return LedgerRepository(engine)


def initialize(root: Path, migrations: Path) -> WorkspaceManifest:
    """Explicit maintenance command: create a NEW synthetic workspace only."""
    root, migrations = absolute_path(root), absolute_path(migrations)
    if not (migrations / "env.py").is_file() or not (migrations / "versions").is_dir():
        raise LocalOfficeBlocked("MIGRATION_SOURCE_REQUIRED")
    config = Config()
    config.set_main_option("script_location", str(migrations).replace("%", "%%"))
    if ScriptDirectory.from_config(config).get_heads() != [HEAD]:
        raise LocalOfficeBlocked("MIGRATION_SOURCE_MISMATCH")
    source = seed_source()
    if source.organization_id != ORGANIZATION:
        raise LocalOfficeBlocked("SYNTHETIC_SOURCE_MISMATCH")
    old_umask = os.umask(0o077)
    try:
        value = create_layout(root, policy_hash=sha256(local_policy()), seed_hash=sha256(source))
        write_new(root / "office.sqlite", b"")
        # migrations/env.py consumes this variable. Initialization is CLI-only;
        # caller configuration is restored and can never redirect the target DB.
        old_url = os.environ.get("SAFE_AGENT_DATABASE_URL")
        os.environ["SAFE_AGENT_DATABASE_URL"] = "sqlite+pysqlite:///" + str(root / "office.sqlite")
        try:
            command.upgrade(config, HEAD)
        finally:
            if old_url is None:
                os.environ.pop("SAFE_AGENT_DATABASE_URL", None)
            else:
                os.environ["SAFE_AGENT_DATABASE_URL"] = old_url
        repo = repository(root)
        try:
            DurableFakeProvider(repo.engine, source)
        finally:
            repo.close()
        finish_layout(root, value)
        return value
    finally:
        os.umask(old_umask)


@dataclass
class LocalOffice:
    root: Path
    manifest: WorkspaceManifest
    repo: LedgerRepository
    broker: ReferenceBroker
    provider: DurableFakeProvider
    calendar: FakeCalendarAdapter
    office: VirtualOffice
    coordinator: TrustedOfficeTasks

    def refresh(self) -> None:
        # Never reuse the original seed as current state. Prior projections retain
        # their original fingerprints and fail closed after a source change.
        self.office.replace_synthetic_source(self.provider.snapshot(ORGANIZATION))

    def close(self) -> None:
        self.repo.close()


def open_office(root: Path) -> LocalOffice:
    root = absolute_path(root)
    value = load_manifest(root)
    seed = seed_source()
    if value.policy_hash != sha256(local_policy()) or value.seed_hash != sha256(seed):
        raise LocalOfficeBlocked("WORKSPACE_CONFIGURATION_CHANGED")
    repo = repository(root)
    try:
        # The constructor is allowed to seed during init only. Detect missing
        # durable state before constructing it on every subsequent startup.
        with repo._sessions() as session:
            orgs = tuple(session.scalars(select(FakeProviderRow.organization_id)))
            if orgs != (ORGANIZATION,):
                raise LocalOfficeBlocked("PERSISTED_PROVIDER_REQUIRED")
        provider = DurableFakeProvider(repo.engine, seed)
        source = provider.snapshot(ORGANIZATION)
        broker = ReferenceBroker(secret=secret(root, "broker.key", value))
        office = VirtualOffice(source, broker)
        calendar = FakeCalendarAdapter(source, durable_store=provider)
        coordinator = TrustedOfficeTasks(repo, office, OfficeGate(broker))
        return LocalOffice(root, value, repo, broker, provider, calendar, office, coordinator)
    except BaseException:
        repo.close()
        raise
