"""Coordinated deployment routes cannot choose legacy preparation or adoption."""
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.storage.coordinated_models import CoordinatedPlanningInputRow
from safe_agent.storage.models import InferenceInvocationRow, OutboxRow
from tests.m2.conftest import NOW
from tests.m6.test_persistent_planning import plan
from tests.m6.test_synthetic_workflow import grant, new_task, prepare, queue
from tests.m7.test_coordinated_planning import build, count


def test_coordinated_api_disables_legacy_prepare(kit, monkeypatch):
    client, signer, actor, calendar, _, _, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    response = prepare(client, signer, actor, task)
    assert response.status_code == 409, response.text
    assert calls == [] and calendar.effect_count == 0
    assert count(kit["repo"], CoordinatedPlanningInputRow) == count(kit["repo"], OutboxRow) == 0
    assert kit["repo"].read_task(task).proposal_hash is None
    # The supported route still performs only planning, never an effect.
    assert plan(client, signer, actor, task).status_code == 200
    assert len(calls) == 1 and calendar.effect_count == 0
    assert count(kit["repo"], OutboxRow) == 0


def test_coordinated_api_does_not_adopt_legacy_prepared_task(kit, monkeypatch):
    client, signer, actor, calendar, internal, service, calls, _ = build(kit, monkeypatch)
    task = new_task(client, signer, actor)
    # Simulate a task prepared by an earlier trusted legacy deployment. This is
    # deliberately not an HTTP mode/permission override available to the caller.
    earlier = service._workflow.prepare(actor, task, expected_state_version=0, now=NOW)
    version = grant(client, signer, kit["manager"], task, earlier.model_dump(mode="json"))
    response = queue(client, signer, actor, task, version=version)
    assert response.status_code == 409, response.text
    assert count(kit["repo"], CoordinatedPlanningInputRow) == 0
    assert count(kit["repo"], InferenceInvocationRow) == count(kit["repo"], OutboxRow) == 0
    worker = SyntheticRecoveryWorker(kit["repo"], kit["broker"], calendar,
                                     task_adapter=internal, office=kit["office"])
    assert worker.process_one(task_id=task, now=NOW) is None
    assert calls == [] and calendar.effect_count == internal.effect_count == 0
