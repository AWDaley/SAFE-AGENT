"""Pinned upstream TAL/SAFE shadow-journal tests; never contact Google."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from safe_agent.contracts.control import ExecutionOutcome
from safe_agent.evidence.ledger import OfficePhase
from safe_agent.execution.recovery import FaultPoint, InjectedCrash, SyntheticRecoveryWorker
from safe_agent.tal.office_audit import TALOfficeAuditSyncError, TrustedTALOfficeAuditMirror
from safe_agent.tal.trusted_dispatch import TrustedTALOfficeDispatchCheck
from tests.m2.conftest import NOW
from tests.m4.test_recovery import stage_calendar

pytest.importorskip("tal.runtime")
journal_module = pytest.importorskip("tal.office_journal")
SQLiteOfficeJournal = journal_module.SQLiteOfficeJournal
OfficeJournalKind = journal_module.OfficeJournalKind


def _worker(kit, adapter, journal):
    return SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], adapter,
        tal_dispatch=TrustedTALOfficeDispatchCheck(office_journal=journal),
    )


def test_verified_safe_receipt_is_replayed_into_tal(tmp_path, kit):
    action_hash, adapter = stage_calendar(kit)
    journal = SQLiteOfficeJournal(tmp_path / "tal.sqlite")
    worker = _worker(kit, adapter, journal)
    receipt = worker.process_one(task_id="task_m2", now=NOW)
    assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED
    mirrored = journal.replay(action_hash)
    assert mirrored.state is OfficeJournalKind.VERIFIED
    assert mirrored.sequence == 2
    assert mirrored.last_evidence_hash is not None
    assert worker.audit_sync_warning("task_m2") is None
    # No new events or effects during later idle reconciliation.
    assert worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=1)) is None
    assert len(journal.events(action_hash)) == 2
    assert adapter.effect_count == 1
    journal.close()


def test_crash_after_write_recovers_same_action_into_tal_journal(tmp_path, kit):
    action_hash, adapter = stage_calendar(kit)
    path = tmp_path / "tal.sqlite"
    journal = SQLiteOfficeJournal(path)
    first = _worker(kit, adapter, journal)
    with pytest.raises(InjectedCrash):
        first.process_one(task_id="task_m2", now=NOW, fault=FaultPoint.AFTER_PROVIDER_COMMIT)
    assert adapter.effect_count == 1
    assert journal.replay(action_hash).state is OfficeJournalKind.ADMISSION
    journal.close()  # Simulate audit service process restart.
    restarted = SQLiteOfficeJournal(path)
    later = _worker(kit, adapter, restarted)
    receipt = later.process_one(task_id="task_m2", now=NOW + timedelta(seconds=31))
    assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED
    assert restarted.replay(action_hash).state is OfficeJournalKind.VERIFIED
    assert len(restarted.events(action_hash)) == 2
    restarted.close()


def test_rate_limit_records_unknown_then_idempotent_verified(tmp_path, kit):
    action_hash, adapter = stage_calendar(kit)
    journal = SQLiteOfficeJournal(tmp_path / "tal.sqlite")
    worker = _worker(kit, adapter, journal)
    first = worker.process_one(task_id="task_m2", now=NOW, fault=FaultPoint.RATE_LIMIT)
    assert first is not None and first.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert adapter.effect_count == 0
    assert journal.replay(action_hash).state is OfficeJournalKind.OUTCOME_UNKNOWN
    assert len(journal.events(action_hash)) == 2
    second = worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=16))
    assert second is not None and second.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    assert journal.replay(action_hash).state is OfficeJournalKind.VERIFIED
    assert len(journal.events(action_hash)) == 3
    journal.close()


def test_audit_postcommit_outage_is_explicit_and_can_be_repaired(tmp_path, kit, monkeypatch):
    action_hash, adapter = stage_calendar(kit)
    journal = SQLiteOfficeJournal(tmp_path / "tal.sqlite")
    worker = _worker(kit, adapter, journal)
    original = TrustedTALOfficeAuditMirror.reconcile

    def unavailable(*_args, **_kwargs):
        raise TALOfficeAuditSyncError("AUDIT_TEMPORARILY_UNAVAILABLE")

    with monkeypatch.context() as mp:
        mp.setattr(TrustedTALOfficeAuditMirror, "reconcile", unavailable)
        receipt = worker.process_one(task_id="task_m2", now=NOW)
    assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
    assert adapter.effect_count == 1
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.COMPLETED
    assert journal.replay(action_hash).state is OfficeJournalKind.ADMISSION
    assert worker.audit_sync_warning("task_m2") == "TAL_AUDIT_SYNC_REQUIRED"
    assert TrustedTALOfficeAuditMirror.reconcile is original
    # Repair operates on SAFE's *previously verified* receipt, not the model or provider.
    assert worker.process_one(task_id="task_m2", now=NOW + timedelta(seconds=1)) is None
    assert journal.replay(action_hash).state is OfficeJournalKind.VERIFIED
    assert worker.audit_sync_warning("task_m2") is None
    assert adapter.effect_count == 1
    journal.close()


def test_tampered_audit_fails_closed_before_first_effect(tmp_path, kit):
    action_hash, adapter = stage_calendar(kit)
    path = tmp_path / "tal.sqlite"
    journal = SQLiteOfficeJournal(path)
    worker = _worker(kit, adapter, journal)
    worker.process_one(task_id="task_m2", now=NOW, fault=FaultPoint.RATE_LIMIT)
    assert adapter.effect_count == 0
    journal.close()
    with sqlite3.connect(path) as raw:
        record = raw.execute(
            "SELECT event_json FROM tal_office_journal WHERE action_hash=? AND sequence=1",
            (action_hash,),
        ).fetchone()[0]
        forged = json.loads(record)
        forged["reason_code"] = "FAKE_APPROVAL"
        raw.execute(
            "UPDATE tal_office_journal SET event_json=? WHERE action_hash=? AND sequence=1",
            (json.dumps(forged), action_hash),
        )
    modified = SQLiteOfficeJournal(path)
    again = _worker(kit, adapter, modified)
    result = again.process_one(task_id="task_m2", now=NOW + timedelta(seconds=16))
    assert result is not None and result.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert adapter.effect_count == 0
    assert kit["repo"].replay_task("task_m2").phase is OfficePhase.NEEDS_REVIEW
    assert again.audit_sync_warning("task_m2") == "TAL_AUDIT_SYNC_REQUIRED"
    modified.close()


def test_existing_non_journalled_completion_cannot_forge_admission(tmp_path, kit):
    action_hash, adapter = stage_calendar(kit)
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], adapter)
    receipt = worker.process_one(task_id="task_m2", now=NOW)
    assert receipt is not None and receipt.outcome is ExecutionOutcome.COMMITTED
    journal = SQLiteOfficeJournal(tmp_path / "tal.sqlite")
    auditor = _worker(kit, adapter, journal)
    assert auditor.process_one(task_id="task_m2", now=NOW + timedelta(seconds=1)) is None
    assert auditor.audit_sync_warning("task_m2") == "TAL_AUDIT_SYNC_REQUIRED"
    assert journal.events(action_hash) == ()
    assert adapter.effect_count == 1
    journal.close()
