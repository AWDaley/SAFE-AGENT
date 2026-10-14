"""M5 opt-in TALRuntime check at M4 outbox dispatch. No Google writes."""
from __future__ import annotations

from datetime import timedelta

import pytest

from safe_agent.contracts.control import ExecutionOutcome
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import (
    FaultPoint,
    InjectedCrash,
    SyntheticRecoveryWorker,
)
from safe_agent.tal.trusted_dispatch import TrustedTALOfficeDispatchCheck
from tests.m2.conftest import NOW, kit  # noqa: F401
from tests.m4.test_recovery import stage_calendar


def test_tal_import_error_blocks_new_synthetic_dispatch(kit, monkeypatch) -> None:
    """Absence of the TAL runtime never silently falls back to local authority."""
    stage_hash, adapter = stage_calendar(kit)

    def unavailable(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("safe_agent.tal.trusted_dispatch.import_module", unavailable)
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], adapter,
        tal_dispatch=TrustedTALOfficeDispatchCheck(),
    )
    receipt = worker.process_one(task_id="task_m2", now=NOW)
    assert receipt is not None and receipt.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert adapter.effect_count == 0
    assert kit["repo"].read_outbox("task_m2")["status"] == "NEEDS_REVIEW"
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert stage_hash == receipt.action_hash


def test_upstream_runtime_allows_one_pinned_synthetic_effect(kit) -> None:
    pytest.importorskip("tal.runtime")
    _, adapter = stage_calendar(kit)
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], adapter,
        tal_dispatch=TrustedTALOfficeDispatchCheck(),
    )
    receipt = worker.process_one(task_id="task_m2", now=NOW)
    assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED
    assert worker.process_one(task_id="task_m2", now=NOW) is None


def test_upstream_failure_does_not_undo_previously_committed_effect(kit, monkeypatch) -> None:
    """For committed effects, reconcile by key/readback without a second grant."""
    _, adapter = stage_calendar(kit)
    before = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    with pytest.raises(InjectedCrash):
        before.process_one(task_id="task_m2", now=NOW,
                           fault=FaultPoint.AFTER_PROVIDER_COMMIT)
    assert adapter.effect_count == 1

    def unavailable(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("safe_agent.tal.trusted_dispatch.import_module", unavailable)
    after = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], adapter,
        tal_dispatch=TrustedTALOfficeDispatchCheck(),
    )
    result = after.process_one(task_id="task_m2", now=NOW + timedelta(seconds=31))
    assert result is not None and result.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED
