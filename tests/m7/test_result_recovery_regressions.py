"""W3 regressions: committed inference must survive caller loss without a resend."""
from sqlalchemy import event

from safe_agent.inference.coordinator import TrustedInferenceCoordinator
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.storage.inference_admission import TrustedInferenceAdmission
from safe_agent.storage.repository import LedgerRepository
from tests.m7.admission_fixtures import NOW, assigned, policy
from tests.m7.test_inference_coordinator import held, transport


def test_restarted_coordinator_recovers_exact_committed_candidate(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'result.sqlite'}"
    repo = LedgerRepository.testing_sqlite(url)
    args = assigned(repo)
    wire = transport()
    calls = []

    def send(request, claim, marker):
        calls.append(request.request_id)
        return OfflineMockInferenceTransport().infer(request)

    monkeypatch.setattr(wire, "send", send)
    first = TrustedInferenceCoordinator(
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW), wire,
    ).execute(*args)
    before = held(repo)
    repo.close()
    restarted = LedgerRepository.testing_sqlite(url)
    try:
        again = TrustedInferenceCoordinator(
            TrustedInferenceAdmission(restarted, policy=policy(), clock=lambda: NOW), wire,
        ).execute(*args)
        assert len(calls) == 1
        assert again.dispatched_this_call is False
        assert again.invocation == first.invocation
        assert first.outcome is not None
        assert again.outcome == first.outcome
        assert held(restarted) == before
    finally:
        restarted.close()


def test_lost_finish_commit_ack_does_not_lose_validated_candidate(tmp_path, monkeypatch):
    repo = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'ack.sqlite'}")
    wire = transport()
    args = assigned(repo)
    calls = []
    armed = [False]

    def lost_ack(session):
        if armed[0]:
            armed[0] = False
            raise RuntimeError("SYNTHETIC_LOST_COMMIT_ACK")

    def send(request, claim, marker):
        calls.append(request.request_id)
        armed[0] = True
        return OfflineMockInferenceTransport().infer(request)

    event.listen(repo._sessions, "after_commit", lost_ack)
    monkeypatch.setattr(wire, "send", send)
    runtime = TrustedInferenceCoordinator(
        TrustedInferenceAdmission(repo, policy=policy(), clock=lambda: NOW), wire,
    )
    try:
        first = runtime.execute(*args)
        again = runtime.execute(*args)
        assert first.invocation.state == again.invocation.state == "RESPONSE_VALIDATED"
        assert len(calls) == 1
        assert again.dispatched_this_call is False
        assert again.outcome is not None
        assert again.outcome.proposal_digest == first.invocation.proposal_hash
    finally:
        event.remove(repo._sessions, "after_commit", lost_ack)
        repo.close()
