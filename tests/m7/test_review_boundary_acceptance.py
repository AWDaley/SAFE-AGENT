"""R1/R2 acceptance with real TAL, real SQL and synthetic provider effects only."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from safe_agent.canonical import sha256
from safe_agent.execution.recovery import FaultPoint, InjectedCrash, SyntheticRecoveryWorker
from safe_agent.storage.models import FakeProviderRow, OutboxRow
from safe_agent.storage.repository import StateConflict
from safe_agent.tal import reviewed_runtime
from safe_agent.tal.trusted_dispatch import TALOfficeDispatchDenied, TrustedTALOfficeDispatchCheck
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import plan
from tests.m6.test_synthetic_workflow import queue
from tests.m7.test_coordinated_planning import build, count, reopened
from tests.m7.test_review_boundary_regressions import change_private_source, prepared


@pytest.mark.parametrize("changed_side", ["office", "provider"])
def test_restore_failure_is_cache_neutral_for_either_source(kit, monkeypatch, changed_side):
    client, signer, actor, calendar, _, _, calls, _, task, _, version = prepared(kit, monkeypatch)
    original = kit["office"]._source
    before = (dict(kit["office"]._observations), dict(kit["office"]._source_fingerprints))
    change_private_source(kit)
    if changed_side == "provider":
        kit["office"].replace_synthetic_source(original)
    else:
        with kit["repo"].engine.begin() as db:
            db.execute(update(FakeProviderRow).where(
                FakeProviderRow.organization_id == original.organization_id,
            ).values(state_json=original.model_dump(mode="json"), state_hash=sha256(original)))
    assert plan(client, signer, actor, task).status_code == 409
    assert (kit["office"]._observations, kit["office"]._source_fingerprints) == before
    assert queue(client, signer, actor, task, version=version).status_code == 409
    assert count(kit["repo"], OutboxRow) == 0 and calendar.effect_count == 0
    assert len(calls) == 1


@pytest.mark.parametrize("restart", [False, True])
def test_real_pinned_tal_checks_new_coordinated_effect(kit, monkeypatch, restart):
    from tal.runtime import TALRuntime

    reviewed_runtime.require_reviewed_tal_runtime()
    client, signer, actor, calendar, internal, _, calls, ticks, task, saved, version = prepared(kit, monkeypatch)
    active = reopened(kit) if restart else kit
    checked = []
    original = TALRuntime.apply_proposal
    def actual_check(self, *args, **kwargs):
        checked.append(args[0])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(TALRuntime, "apply_proposal", actual_check)
    try:
        if restart:
            client, signer, actor, calendar, internal, _, _, _ = build(
                active, monkeypatch, calls=calls, ticks=ticks,
            )
            assert plan(client, signer, actor, task).status_code == 200
        assert active["office"].trusted_projection_fingerprint(saved.projection, now=NOW) == saved.source_fingerprint
        assert queue(client, signer, actor, task, version=version).status_code == 200
        with active["repo"]._sessions() as db:
            outbox = db.scalars(select(OutboxRow)).one()
            assert outbox.source_hash == saved.source_fingerprint
        worker = SyntheticRecoveryWorker(
            active["repo"], active["broker"], calendar, task_adapter=internal,
            office=active["office"], tal_dispatch=TrustedTALOfficeDispatchCheck(),
        )
        receipt = worker.process_one(task_id=task, now=NOW)
        assert receipt is not None and receipt.outcome == "COMMITTED"
        assert worker.process_one(task_id=task, now=NOW) is None
        assert calendar.effect_count == 1 and checked == [task] and len(calls) == 1
    finally:
        if restart:
            active["repo"].close()


@pytest.mark.parametrize("bad_checker", ["object", "subclass"])
def test_incompatible_checker_cannot_authorize_coordinated_effect(kit, monkeypatch, bad_checker):
    client, signer, actor, calendar, internal, _, calls, _, task, _, version = prepared(kit, monkeypatch)
    assert queue(client, signer, actor, task, version=version).status_code == 200
    class NoOpSubclass(TrustedTALOfficeDispatchCheck):
        def require(self, **kwargs):
            raise AssertionError("an incompatible checker must not be called")
    checker = NoOpSubclass() if bad_checker == "subclass" else object()
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], calendar, task_adapter=internal,
        office=kit["office"], tal_dispatch=checker,
    )
    receipt = worker.process_one(task_id=task, now=NOW)
    assert receipt is not None and receipt.outcome != "COMMITTED"
    assert calendar.effect_count == internal.effect_count == 0 and len(calls) == 1
    assert kit["repo"].read_outbox(task)["status"] == "NEEDS_REVIEW"


@pytest.mark.parametrize("failure", ["import", "denying", "incompatible", "unreviewed_pin"])
def test_missing_denying_or_incompatible_tal_blocks_new_effect(kit, monkeypatch, failure):
    from tal.runtime import TALRuntime

    client, signer, actor, calendar, internal, _, calls, _, task, _, version = prepared(kit, monkeypatch)
    assert queue(client, signer, actor, task, version=version).status_code == 200
    observed = []
    if failure == "import":
        def unavailable(name):
            observed.append(name)
            raise ImportError("synthetic unavailable TAL module")
        monkeypatch.setattr("safe_agent.tal.trusted_dispatch.import_module", unavailable)
    elif failure == "unreviewed_pin":
        class WrongRevision:
            def read_text(self, _):
                observed.append("metadata")
                return json.dumps({"url": reviewed_runtime.REVIEWED_TAL_REPOSITORY,
                                   "vcs_info": {"vcs": "git", "commit_id": "0" * 40}})
        monkeypatch.setattr(reviewed_runtime, "distribution", lambda _: WrongRevision())
    else:
        def refused(*args, **kwargs):
            observed.append("runtime")
            return SimpleNamespace(status="DENIED") if failure == "denying" else object()
        monkeypatch.setattr(TALRuntime, "apply_proposal", refused)
    checker = TrustedTALOfficeDispatchCheck()
    # An instance no-op must not override the class's real enforcement path.
    monkeypatch.setattr(checker, "require", lambda **kwargs: None)
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], calendar, task_adapter=internal,
        office=kit["office"], tal_dispatch=checker,
    )
    receipt = worker.process_one(task_id=task, now=NOW)
    assert observed, "the selected TAL failure was actually reached"
    assert receipt is not None and receipt.outcome != "COMMITTED"
    assert calendar.effect_count == internal.effect_count == 0 and len(calls) == 1
    assert kit["repo"].read_outbox(task)["status"] == "NEEDS_REVIEW"


def test_committed_effect_readback_needs_no_new_tal_authority(kit, monkeypatch):
    client, signer, actor, calendar, internal, _, calls, ticks, task, _, version = prepared(kit, monkeypatch)
    assert queue(client, signer, actor, task, version=version).status_code == 200
    worker = SyntheticRecoveryWorker(
        kit["repo"], kit["broker"], calendar, task_adapter=internal,
        office=kit["office"], tal_dispatch=TrustedTALOfficeDispatchCheck(),
    )
    with pytest.raises(InjectedCrash):
        worker.process_one(task_id=task, now=NOW, fault=FaultPoint.AFTER_PROVIDER_COMMIT)
    assert calendar.effect_count == 1
    fresh = reopened(kit)
    try:
        _, _, _, calendar, internal, _, _, _ = build(fresh, monkeypatch, calls=calls, ticks=ticks)
        def forbidden():
            raise AssertionError("readback must not renew TAL authority")
        monkeypatch.setattr("safe_agent.execution.recovery.require_reviewed_tal_runtime", forbidden)
        after = SyntheticRecoveryWorker(
            fresh["repo"], fresh["broker"], calendar, task_adapter=internal, office=fresh["office"],
        )
        # Both the original inference deadline and the projection have expired.
        later = NOW + timedelta(minutes=11)
        receipt = after.process_one(task_id=task, now=later)
        assert receipt is not None and receipt.outcome == "COMMITTED"
        assert after.process_one(task_id=task, now=later) is None
        assert calendar.effect_count == 1 and len(calls) == 1
    finally:
        fresh["repo"].close()


def test_legacy_inline_gateway_cannot_bypass_coordinated_worker(kit, monkeypatch):
    _, _, actor, calendar, _, service, calls, _, task, _, version = prepared(kit, monkeypatch)
    with pytest.raises(StateConflict, match="COORDINATED_TAL_WORKER_REQUIRED"):
        service._workflow._gateway.execute(actor, task, now=NOW, expected_state_version=version)
    assert calendar.effect_count == 0 and count(kit["repo"], OutboxRow) == 0
    assert len(calls) == 1


def test_runtime_pin_matches_reviewed_release_pin():
    root = Path(__file__).resolve().parents[2]
    assert reviewed_runtime.REVIEWED_TAL_COMMIT == (root / "ci/tal-office-commit.sha").read_text().strip()
    reviewed_runtime.require_reviewed_tal_runtime()


@pytest.mark.parametrize("metadata", [None, {}, {"url": "https://invalid.example/other.git"}])
def test_absent_or_foreign_installation_metadata_is_denied(monkeypatch, metadata):
    class Metadata:
        def read_text(self, _):
            return json.dumps(metadata)
    monkeypatch.setattr(reviewed_runtime, "distribution", lambda _: Metadata())
    with pytest.raises(TALOfficeDispatchDenied, match="TAL_REVIEWED_RUNTIME_REQUIRED"):
        reviewed_runtime.require_reviewed_tal_runtime()
