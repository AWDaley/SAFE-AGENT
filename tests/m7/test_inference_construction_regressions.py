"""Unsupported inference construction must stop before any backend call."""
import pytest

from safe_agent.inference import InferenceGateway, OfflineMockInferenceTransport
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
