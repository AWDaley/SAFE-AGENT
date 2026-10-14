"""Durable fake-provider state, distinct from the SAFE-AGENT authority ledger.

This is a synthetic *test provider*. It supplies native idempotency by writing the
provider state and action-key registry atomically in its own DB transaction.
It intentionally cannot access credentials or a live provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import sessionmaker

from safe_agent.canonical import sha256
from safe_agent.storage.models import FakeProviderEffectRow, FakeProviderRow
from safe_agent.virtual_office.state import SyntheticOfficeState


class ProviderIntegrityError(ValueError):
    """Stale provider state, tampering, or attempted idempotency-key reuse."""


@dataclass(frozen=True)
class AppliedEffect:
    effect_ref: str
    was_new: bool


class DurableFakeProvider:
    """Atomic fixture store with provider-owned idempotency and CAS concurrency.

    A reconnect uses the stored provider snapshot, *never* re-seeds from the
    original fixture, which would otherwise erase committed work on restart.
    """

    supports_idempotency = True

    def __init__(self, engine: Engine, source: SyntheticOfficeState) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with self._sessions.begin() as session:
            row = session.get(FakeProviderRow, source.organization_id)
            if row is None:
                session.add(FakeProviderRow(
                    organization_id=source.organization_id,
                    generation=0,
                    state_json=source.model_dump(mode="json"),
                    state_hash=sha256(source),
                ))
            else:
                self._read(row)  # Never replace existing authoritative state.

    @staticmethod
    def _read(row: FakeProviderRow) -> SyntheticOfficeState:
        source = SyntheticOfficeState.model_validate_json(json.dumps(row.state_json))
        if source.organization_id != row.organization_id or sha256(source) != row.state_hash:
            raise ProviderIntegrityError("durable provider snapshot integrity mismatch")
        return source

    def snapshot(self, organization_id: str) -> SyntheticOfficeState:
        with self._sessions() as session:
            row = session.get(FakeProviderRow, organization_id)
            if row is None:
                raise ProviderIntegrityError("unknown synthetic provider organization")
            return self._read(row)

    def find_effect(self, *, organization_id: str, action_hash: str,
                    operation: Literal["calendar.create_event", "tasks.create"] | None = None,
                    request_hash: str | None = None) -> str | None:
        with self._sessions() as session:
            record = session.get(FakeProviderEffectRow, action_hash)
            if record is None:
                return None
            if record.organization_id != organization_id:
                raise ProviderIntegrityError("cross-organization action identity")
            if operation is not None and record.operation != operation:
                raise ProviderIntegrityError("action operation mismatch")
            if request_hash is not None and record.request_hash != request_hash:
                raise ProviderIntegrityError("action key reused for different payload")
            # A committed provider effect must not be considered valid if the
            # backing provider snapshot was lost/tampered with.
            provider = session.get(FakeProviderRow, organization_id)
            if provider is None:
                raise ProviderIntegrityError("provider state not found")
            self._read(provider)
            return record.effect_ref

    def apply(
        self, *, organization_id: str, action_hash: str,
        operation: Literal["calendar.create_event", "tasks.create"],
        request_hash: str,
        mutation: Callable[[SyntheticOfficeState], tuple[SyntheticOfficeState, str]],
    ) -> AppliedEffect:
        """Serialize a conditional provider write and its idempotency receipt."""
        with self._sessions.begin() as session:
            previous = session.get(FakeProviderEffectRow, action_hash)
            if previous is not None:
                if (previous.organization_id != organization_id or
                        previous.operation != operation or previous.request_hash != request_hash):
                    raise ProviderIntegrityError("action key reused for different effect")
                state_row = session.get(FakeProviderRow, organization_id)
                if state_row is None:
                    raise ProviderIntegrityError("missing fake provider state")
                self._read(state_row)
                return AppliedEffect(previous.effect_ref, False)

            row = session.scalar(select(FakeProviderRow).where(
                FakeProviderRow.organization_id == organization_id,
            ).with_for_update())
            if row is None:
                raise ProviderIntegrityError("unknown provider organization")
            current = self._read(row)
            next_state, effect_ref = mutation(current)
            if (next_state.organization_id != organization_id or
                    next_state == current or not effect_ref):
                raise ProviderIntegrityError("invalid synthetic provider mutation")
            # SQLite ignores FOR UPDATE; the generation CAS prevents lost updates.
            applied = session.execute(update(FakeProviderRow).where(
                FakeProviderRow.organization_id == organization_id,
                FakeProviderRow.generation == row.generation,
            ).values(generation=row.generation + 1,
                     state_json=next_state.model_dump(mode="json"),
                     state_hash=sha256(next_state)).returning(FakeProviderRow.organization_id))
            if applied.scalar_one_or_none() != organization_id:
                raise ProviderIntegrityError("concurrent provider state changed")
            session.add(FakeProviderEffectRow(
                action_hash=action_hash,
                organization_id=organization_id,
                operation=operation,
                request_hash=request_hash,
                effect_ref=effect_ref,
            ))
            session.flush()
            return AppliedEffect(effect_ref, True)
