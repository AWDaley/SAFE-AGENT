"""Unsupported inference construction must stop before any backend call."""
import pytest

from safe_agent.inference import InferenceDenied, InferenceGateway, OfflineMockInferenceTransport
from tests.m2.conftest import NOW
from tests.m7.test_inference_gateway import fixture, rule


class NonMockBackend:
    def __init__(self):
        self.calls = 0

    def infer(self, request):
        self.calls += 1
        return OfflineMockInferenceTransport().infer(request)


def test_legacy_gateway_rejects_non_mock_without_durable_admission():
    backend = NonMockBackend()
    with pytest.raises(TypeError, match="coordinated"):
        InferenceGateway(rule(), backend, clock=lambda: NOW)
    assert backend.calls == 0


def test_offline_fixture_path_remains_usable():
    gateway = InferenceGateway(rule(), OfflineMockInferenceTransport(), clock=lambda: NOW)
    result = gateway.infer(fixture()[2])
    assert result.provider_effect_performed is False
    assert result.model_authorized_execution is False


def test_backend_substitution_after_construction_is_denied():
    backend = NonMockBackend()
    gateway = InferenceGateway(rule(), OfflineMockInferenceTransport(), clock=lambda: NOW)
    gateway._transport = backend
    with pytest.raises(InferenceDenied, match="INFERENCE_SCOPE_DENIED"):
        gateway.infer(fixture()[2])
    assert backend.calls == 0


@pytest.mark.parametrize("mode", ["coordinated_offline", "hosted", "", None])
def test_legacy_mode_flag_does_not_enable_an_application_path(mode):
    with pytest.raises(TypeError, match="coordinated"):
        InferenceGateway(rule(), OfflineMockInferenceTransport(), clock=lambda: NOW, mode=mode)
