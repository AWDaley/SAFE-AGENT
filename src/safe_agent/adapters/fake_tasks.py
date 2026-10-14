"""Synthetic-only internal office task list, with versioned conditional writes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Literal

from safe_agent.canonical import sha256
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.virtual_office.state import PrivateInternalTask, SyntheticOfficeState


class FakeTaskConflict(ValueError):
    """Trusted synthetic source changed or requested effect is invalid."""


@dataclass(frozen=True)
class TrustedTaskListRead:
    revision: str
    source_hash: str


class FakeInternalTaskReader:
    def __init__(self, adapter: FakeInternalTaskAdapter) -> None:
        self.__adapter = adapter

    def snapshot(self, *, organization_id: str) -> TrustedTaskListRead:
        return self.__adapter._snapshot(organization_id)

    def task(self, *, organization_id: str, task_ref: str) -> PrivateInternalTask | None:
        return self.__adapter._task(organization_id, task_ref)


class FakeInternalTaskWriter:
    def __init__(self, adapter: FakeInternalTaskAdapter) -> None:
        self.__adapter = adapter

    def create(self, *, organization_id: str, customer_id: str,
               task_code: Literal["call_back", "prepare_documents", "review_request"],
               due_at: datetime, now: datetime, action_hash: str,
               expected_revision: str, expected_source_hash: str) -> str:
        return self.__adapter._commit(
            organization_id=organization_id, customer_id=customer_id,
            task_code=task_code, due_at=due_at, now=now, action_hash=action_hash,
            expected_revision=expected_revision, expected_source_hash=expected_source_hash,
        )


class FakeInternalTaskAdapter:
    """Privileged test-only task store. Never accessible through planner or API."""

    def __init__(self, source: SyntheticOfficeState, *,
                 durable_store: DurableFakeProvider | None = None) -> None:
        self.__state = source
        self._durable = durable_store
        self.__lock = RLock()
        self.__effects: set[str] = set()
        self.__effect_count = 0
        self.__readback_error = False

    @property
    def supports_idempotency(self) -> bool:
        return self._durable is not None

    def find_effect(self, *, organization_id: str, action_hash: str,
                    request_hash: str | None = None) -> str | None:
        if self._durable is not None:
            return self._durable.find_effect(
                organization_id=organization_id, action_hash=action_hash,
                operation="tasks.create", request_hash=request_hash,
            )
        if organization_id != self.__state.organization_id:
            raise FakeTaskConflict("organization mismatch")
        return None

    @property
    def effect_count(self) -> int:
        with self.__lock:
            if self._durable is not None:
                from sqlalchemy import func, select
                from safe_agent.storage.models import FakeProviderEffectRow
                with self._durable._sessions() as session:
                    return int(session.scalar(select(func.count()).select_from(
                        FakeProviderEffectRow).where(
                            FakeProviderEffectRow.organization_id == self.__state.organization_id,
                            FakeProviderEffectRow.operation == "tasks.create",
                        )) or 0)
            return self.__effect_count

    def reader(self) -> FakeInternalTaskReader:
        return FakeInternalTaskReader(self)

    def trusted_writer(self) -> FakeInternalTaskWriter:
        return FakeInternalTaskWriter(self)

    def synthetic_state(self) -> SyntheticOfficeState:
        with self.__lock:
            if self._durable is not None:
                return self._durable.snapshot(self.__state.organization_id)
            return self.__state

    def inject_readback_failure(self, value: bool) -> None:
        self.__readback_error = value

    def replace_source_for_test(self, state: SyntheticOfficeState) -> None:
        if state.organization_id != self.__state.organization_id:
            raise FakeTaskConflict("cross-organization fake task source")
        with self.__lock:
            if self._durable is not None:
                raise FakeTaskConflict("durable provider cannot be overwritten by test hook")
            self.__state = state

    def _snapshot(self, organization_id: str) -> TrustedTaskListRead:
        with self.__lock:
            state = self.synthetic_state()
            if organization_id != state.organization_id:
                raise FakeTaskConflict("organization mismatch")
            return TrustedTaskListRead(
                revision=state.task_list_revision,
                source_hash=sha256(state.internal_tasks),
            )

    def _task(self, organization_id: str, task_ref: str) -> PrivateInternalTask | None:
        if self.__readback_error:
            raise ConnectionError("synthetic task read-back unavailable")
        with self.__lock:
            source = self.synthetic_state()
            if organization_id != source.organization_id:
                raise FakeTaskConflict("organization mismatch")
            return next((t for t in source.internal_tasks
                         if t.task_id == task_ref and t.organization_id == organization_id), None)

    def _commit(self, *, organization_id: str, customer_id: str,
                task_code: Literal["call_back", "prepare_documents", "review_request"],
                due_at: datetime, now: datetime, action_hash: str,
                expected_revision: str, expected_source_hash: str) -> str:
        if self._durable is not None:
            request_hash = sha256({
                "organization_id": organization_id, "customer_id": customer_id,
                "task_code": task_code, "due_at": due_at,
                "expected_revision": expected_revision,
                "expected_source_hash": expected_source_hash,
            })

            def mutate(source: SyntheticOfficeState) -> tuple[SyntheticOfficeState, str]:
                ephemeral = FakeInternalTaskAdapter(source)
                ref = ephemeral._commit(
                    organization_id=organization_id, customer_id=customer_id,
                    task_code=task_code, due_at=due_at, now=now,
                    action_hash=action_hash, expected_revision=expected_revision,
                    expected_source_hash=expected_source_hash,
                )
                return ephemeral.synthetic_state(), ref

            return self._durable.apply(
                organization_id=organization_id, action_hash=action_hash,
                operation="tasks.create", request_hash=request_hash, mutation=mutate,
            ).effect_ref
        with self.__lock:
            snapshot = self._snapshot(organization_id)
            if (snapshot.revision != expected_revision
                    or snapshot.source_hash != expected_source_hash):
                raise FakeTaskConflict("stale synthetic task source")
            if action_hash in self.__effects:
                raise FakeTaskConflict("duplicate fake task action")
            if due_at <= now:
                raise FakeTaskConflict("task due date must be in the future")
            try:
                customer = self.__state.customer(customer_id)
            except StopIteration as error:
                raise FakeTaskConflict("unknown customer") from error
            if customer.organization_id != organization_id:
                raise FakeTaskConflict("cross-organization customer")
            if any(t.customer_id == customer_id and t.task_code == task_code
                   and t.due_at == due_at for t in self.__state.internal_tasks):
                raise FakeTaskConflict("duplicate synthetic task")
            ref = "fake_task_" + sha256({
                "action_hash": action_hash, "organization": organization_id,
            })[7:39]
            entry = PrivateInternalTask(
                task_id=ref, organization_id=organization_id, customer_id=customer_id,
                task_code=task_code, status="open", due_at=due_at,
            )
            revision = "revision_" + sha256({
                "previous": snapshot.revision, "effect": entry,
            })[7:39]
            self.__state = self.__state.model_copy(update={
                "task_list_revision": revision,
                "internal_tasks": (*self.__state.internal_tasks, entry),
            })
            self.__effects.add(action_hash)
            self.__effect_count += 1
            return ref
