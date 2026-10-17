"""Application construction never unlocks a stateless or arbitrary backend."""
import pytest

from safe_agent.inference.bootstrap import create_application_inference
from safe_agent.inference.mock import OfflineMockInferenceTransport
from safe_agent.storage.repository import LedgerRepository
from tests.m7.test_inference_coordinator import configured, held, transport


@pytest.fixture
def repository(tmp_path):
    repo = LedgerRepository.testing_sqlite(f"sqlite+pysqlite:///{tmp_path / 'bootstrap.sqlite'}")
    yield repo
    repo.close()


@pytest.mark.parametrize("mode", ["offline_fixture", "hosted", "", None])
def test_unsupported_application_mode_cannot_construct(repository, mode):
    _, _, admission, _ = configured(repository)
    with pytest.raises(TypeError, match="coordinated"):
        create_application_inference(mode=mode, admission=admission, transport=transport())


def test_application_requires_exact_admission_and_transport(repository):
    _, _, admission, _ = configured(repository)
    with pytest.raises(TypeError):
        create_application_inference(mode="coordinated_offline", transport=transport())
    with pytest.raises(TypeError):
        create_application_inference(mode="coordinated_offline", admission=None,
                                     transport=transport())
    with pytest.raises(TypeError):
        create_application_inference(mode="coordinated_offline", admission=admission,
                                     transport=OfflineMockInferenceTransport())


def test_application_retains_attempt_identity_and_never_resends(repository, monkeypatch):
    args, _, admission, _ = configured(repository)
    wire = transport()
    calls = []
    def send(request, claim, marker):
        calls.append(request.request_id)
        return OfflineMockInferenceTransport().infer(request)
    monkeypatch.setattr(wire, "send", send)
    runtime = create_application_inference(mode="coordinated_offline",
                                           admission=admission, transport=wire)
    before = held(repository)
    first = runtime.execute(*args)
    again = runtime.execute(*args)
    assert first.invocation.state == again.invocation.state == "RESPONSE_VALIDATED"
    assert first.invocation == again.invocation
    assert len(calls) == 1
    assert again.dispatched_this_call is False
    assert held(repository) == before
    assert not hasattr(runtime, "generate")
