"""W4 abrupt application exit: real processes, synthetic data and controlled time.

These tests do not claim live-model, power-loss or production isolation proof.
A separate caller recovers by task ID, without the first caller's memory/prompt.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import event, update

from safe_agent.evidence.ledger import OfficePhase
from safe_agent.storage.coordinated_models import (
    CoordinatedPlanningConsumptionRow as ConsumptionRow,
)
from safe_agent.storage.models import InferenceInvocationRow, PlanningOutcomeRow, ReferenceRow
from tests.m6.test_persistent_planning import plan
from tests.m6.test_synthetic_workflow import new_task
from tests.m7.test_coordinated_planning import build, count

ROOT = Path(__file__).resolve().parents[2]
CHILD = r'''
import json
import os
import sys
from pathlib import Path

import pytest

from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.storage.coordinated_models import CoordinatedPlanningConsumptionRow, CoordinatedPlanningInputRow
from safe_agent.storage.models import InferenceInvocationRow, OutboxRow, PlanningAttemptRow
from safe_agent.storage.result_models import InferenceValidatedResultRow
from tests.m2.conftest import NOW, kit
from tests.m6.test_persistent_planning import plan
from tests.m6.test_synthetic_workflow import grant, new_task, queue
from tests.m7.test_coordinated_planning import build, count
from tests.m7.test_inference_coordinator import held

path = Path(sys.argv[1])
mode = sys.argv[2]
fixture = kit.__wrapped__(path)
state = next(fixture)
patch = pytest.MonkeyPatch()
try:
    client, signer, actor, calendar, internal, service, calls, ticks = build(state, patch)
    journal = path / "inference_calls.txt"
    if mode != "resume":
        task = new_task(client, signer, actor)
        (path / "task.json").write_text(json.dumps({"task_id": task}), encoding="utf-8")
        wire = service._inference._transport
        send = wire.send
        def counted(*args):
            with journal.open("a", encoding="ascii") as log:
                log.write(args[0].request_id + "\n")
                log.flush()
                os.fsync(log.fileno())
            return send(*args)
        patch.setattr(wire, "send", counted)
        consume = service._consume
        def exit_at_boundary(*args):
            if mode == "after_consumption":
                consume(*args)
            os._exit(73)
        patch.setattr(service, "_consume", exit_at_boundary)
        plan(client, signer, actor, task)
        raise AssertionError("caller never reached the requested crash boundary")
    task = json.loads((path / "task.json").read_text(encoding="utf-8"))["task_id"]
    before = held(state["repo"])
    def forbidden(*args):
        with journal.open("a", encoding="ascii") as log:
            log.write("FORBIDDEN_REDISPATCH\n")
        raise AssertionError("recovery must not invoke inference")
    patch.setattr(service._inference._transport, "send", forbidden)
    response = plan(client, signer, actor, task)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PREPARED"
    assert plan(client, signer, actor, task).json() == response.json()
    assert held(state["repo"]) == before
    for table in (CoordinatedPlanningConsumptionRow, CoordinatedPlanningInputRow,
                  InferenceInvocationRow, PlanningAttemptRow, InferenceValidatedResultRow):
        assert count(state["repo"], table) == 1
    assert count(state["repo"], OutboxRow) == 0 and calendar.effect_count == 0
    approved_version = grant(client, signer, state["manager"], task, response.json())
    assert queue(client, signer, actor, task, version=approved_version).status_code == 200
    worker = SyntheticRecoveryWorker(state["repo"], state["broker"], calendar,
                                     task_adapter=internal, office=state["office"])
    result = worker.process_one(task_id=task, now=NOW)
    assert result is not None and result.outcome == "COMMITTED"
    assert worker.process_one(task_id=task, now=NOW) is None
    assert calendar.effect_count == 1
    assert len(journal.read_text(encoding="ascii").splitlines()) == 1
    print("W4_PROCESS_RESTART_SINGLE_INFERENCE=PASS")
finally:
    patch.undo()
    fixture.close()
'''


@pytest.mark.parametrize("boundary", ["after_result", "after_consumption"])
def test_abrupt_application_restart_preserves_plan_and_single_inference(tmp_path, boundary):
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    first = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path), boundary],
                           cwd=ROOT, env=environment, capture_output=True, text=True,
                           timeout=30, check=False)
    assert first.returncode == 73, first.stdout[-2000:] + first.stderr[-2000:]
    second = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path), "resume"],
                            cwd=ROOT, env=environment, capture_output=True, text=True,
                            timeout=30, check=False)
    assert second.returncode == 0, second.stdout[-2000:] + second.stderr[-2000:]
    assert "W4_PROCESS_RESTART_SINGLE_INFERENCE=PASS" in second.stdout
    assert len((tmp_path / "inference_calls.txt").read_text(encoding="ascii").splitlines()) == 1


def test_saved_input_revoked_before_admission_never_calls_inference(kit, monkeypatch):
    client, signer, actor, _, _, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    execute = service._inference.execute
    def stop(*_):
        raise RuntimeError("stop before admission")
    monkeypatch.setattr(service._inference, "execute", stop)
    assert plan(client, signer, actor, task).status_code == 409
    monkeypatch.setattr(service._inference, "execute", execute)
    with kit["repo"].engine.begin() as db:
        db.execute(update(ReferenceRow).where(ReferenceRow.task_id == task).values(revoked=True))
    assert plan(client, signer, actor, task).status_code == 409
    assert calls == [] and count(kit["repo"], InferenceInvocationRow) == 0


def test_slow_consumption_transaction_rolls_back_when_deadline_passes(kit, monkeypatch):
    from datetime import timedelta

    client, signer, actor, _, _, _, calls, ticks = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    def slow(*_):
        ticks[0] += timedelta(seconds=11)
    event.listen(ConsumptionRow, "before_insert", slow)
    try:
        assert plan(client, signer, actor, task).status_code == 409
    finally:
        event.remove(ConsumptionRow, "before_insert", slow)
    assert len(calls) == 1
    assert count(kit["repo"], ConsumptionRow) == count(kit["repo"], PlanningOutcomeRow) == 0
    assert kit["repo"].replay_task(task).phase is OfficePhase.ASSIGNED
