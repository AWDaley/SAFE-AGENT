"""W3 real-process restart and independently exercised result write guards.

Only synthetic fixtures and mock inference are used. The subprocess deliberately
exits after a committed result; its successor has no callable inference backend.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import DBAPIError

from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.actions import parse_proposal_json
from safe_agent.contracts.inference import InferenceOutcome
from safe_agent.contracts.inference_results import seal_result
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.storage.inference_dispatch import InferenceDispatchDenied
from safe_agent.storage.inference_results import recover_validated_result
from safe_agent.storage.result_models import InferenceValidatedResultRow as ResultRow
from tests.m7 import test_inference_results as result_cases
from tests.m7.test_inference_coordinator import configured, held, transport

repo = result_cases.repo
ROOT = Path(__file__).resolve().parents[2]


def test_competing_finishers_record_only_one_result(repo):
    args, _, admission, store = configured(repo)
    claim = store.claim(*args)
    request, _ = store.start(*args, claim)
    before = held(repo)
    reply = OfflineMockInferenceTransport().infer(request)
    barrier = Barrier(2)

    def finish(_):
        barrier.wait(timeout=10)
        try:
            return store.finish(*args, claim, reply)
        except InferenceDispatchDenied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    assert sum(result is not None for result in results) == 1
    with repo._sessions() as db:
        assert len(db.scalars(select(ResultRow)).all()) == 1
    assert recover_validated_result(admission, *args[:2]).status == "AVAILABLE"
    assert held(repo) == before


def test_sql_result_metadata_must_match_validation_event(repo):
    args, _, admission, store = configured(repo)
    claim = store.claim(*args)
    request, _ = store.start(*args, claim)
    proposal = parse_proposal_json(OfflineMockInferenceTransport().infer(request).output_json)
    outcome = InferenceOutcome(request_id=request.request_id, route_id=request.route_id,
                               model_revision=request.model_revision, prompt_digest=request.prompt_digest,
                               proposal=proposal, proposal_digest=sha256(proposal))
    with store._locked(*args) as locked:
        view = store._transition(locked, "RESPONSE_VALIDATED", proposal_hash=outcome.proposal_digest)
        # SQLite DateTime discards offsets: use the same UTC value as the event,
        # exactly as the production finish transaction does.
        validated_at = locked.now
    record = seal_result(binding=claim.binding, lease=args[1], policy_hash=admission._policy_hash,
                         request=request, outcome=outcome, validation_event_hash=view.latest_event_hash,
                         validated_at=validated_at)
    valid = dict(request_id=request.request_id, binding_hash=claim.binding.binding_hash,
                 organization_id=request.task.organization_id, task_id=request.task.task_id,
                 attempt_id=args[1].attempt_id, policy_hash=admission._policy_hash,
                 candidate_hash=outcome.proposal_digest, validation_sequence=view.event_sequence,
                 validation_event_hash=view.latest_event_hash, validated_at=validated_at,
                 result_hash=record.result_hash, record_json=canonical_json(record).decode())
    for field, wrong in (
        ("organization_id", "org_forged"), ("task_id", "task_forged"),
        ("attempt_id", "attempt_forged"), ("policy_hash", "sha256:" + "0" * 64),
        ("candidate_hash", "sha256:" + "0" * 64),
        ("validation_event_hash", "sha256:" + "0" * 64),
        ("validated_at", validated_at + timedelta(seconds=1)),
    ):
        with pytest.raises(DBAPIError):
            with repo.engine.begin() as db:
                db.execute(insert(ResultRow).values(**{**valid, field: wrong}))
    with repo._sessions() as db:
        assert db.scalars(select(ResultRow)).all() == []
    assert recover_validated_result(admission, *args[:2]).status == "LEGACY_MISSING"
    with repo.engine.begin() as db:
        db.execute(insert(ResultRow).values(**valid))
    assert recover_validated_result(admission, *args[:2]).outcome == outcome


def test_unadmitted_recovery_cannot_create_records_or_call_transport(repo, monkeypatch):
    from safe_agent.storage.inference_admission import TrustedInferenceAdmission
    from safe_agent.storage.inference_results import InferenceResultDenied
    from tests.m7.admission_fixtures import NOW, assigned, policy

    args = assigned(repo)
    wire = transport()
    calls = []
    monkeypatch.setattr(wire, "send", lambda *a: calls.append(a))
    runtime = TrustedInferenceCoordinator(
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW), wire,
    )
    with pytest.raises(InferenceResultDenied):
        runtime.resume(*args[:2])
    assert calls == [] and held(repo) == []


_PROCESS = r'''
import json
import os
import sys
from pathlib import Path
from safe_agent.contracts.planning import PlanningLease
from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.security.principal import Principal
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy
from tests.m7.test_inference_coordinator import held, transport

root = Path(sys.argv[2])
repo = LedgerRepository.testing_sqlite('sqlite+pysqlite:///' + str(root / 'durable.sqlite'))
wire = transport()
service = TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW)
runtime = TrustedInferenceCoordinator(service, wire)
if sys.argv[1] == 'writer':
    args = assigned(repo)
    calls = []
    def send(request, claim, marker):
        calls.append(request.request_id)
        return OfflineMockInferenceTransport().infer(request)
    wire.send = send
    receipt = runtime.execute(*args)
    assert receipt.outcome is not None and len(calls) == 1
    evidence = dict(principal=args[0].model_dump(mode='json'), lease=args[1].model_dump(mode='json'),
                    outcome=receipt.outcome.model_dump(mode='json'), held=held(repo))
    with (root / 'evidence.json').open('w') as f:
        json.dump(evidence, f)
        f.flush()
        os.fsync(f.fileno())
    os._exit(73)  # No object destructors, repo.close(), or normal caller cleanup.
else:
    evidence = json.loads((root / 'evidence.json').read_text())
    principal = Principal.model_validate_json(json.dumps(evidence['principal']))
    lease = PlanningLease.model_validate_json(json.dumps(evidence['lease']))
    def forbidden(*a):
        raise AssertionError('RECOVERY_MUST_NOT_CALL_INFERENCE')
    wire.send = forbidden
    result = runtime.resume(principal, lease)  # No original prompt supplied.
    assert result.status == 'AVAILABLE' and result.source_snapshot is not None
    assert result.outcome.model_dump(mode='json') == evidence['outcome']
    assert result.dispatched_this_call is False
    assert held(repo) == evidence['held']
    repo.close()
    print('W3_ABRUPT_CALLER_RESTART_RESULT_REUSE=PASS')
'''


def test_result_survives_abrupt_process_exit_without_original_prompt(tmp_path):
    writer = subprocess.run(
        [sys.executable, "-c", _PROCESS, "writer", str(tmp_path)],
        cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=False,
    )
    assert writer.returncode == 73, writer.stdout + writer.stderr
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert "prompt" not in evidence
    reader = subprocess.run(
        [sys.executable, "-c", _PROCESS, "reader", str(tmp_path)],
        cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=False,
    )
    assert reader.returncode == 0, reader.stdout + reader.stderr
    assert "W3_ABRUPT_CALLER_RESTART_RESULT_REUSE=PASS" in reader.stdout
