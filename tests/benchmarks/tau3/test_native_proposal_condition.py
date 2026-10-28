"""Opt-in proposal presentation is a bound condition, never a customer or gate bypass."""

from copy import deepcopy

import pytest
from pydantic import SecretStr

from safe_agent.benchmarks.tau3 import preflight, qualification, reporting
from safe_agent.benchmarks.tau3.config import BenchmarkConfig, harness_model_settings
from safe_agent.contracts.episode_inference import (
    EpisodeBackendCapabilities,
    EpisodeBackendRoute,
    EpisodeServiceCapabilities,
    LocalTimingProfile,
)
from safe_agent.inference.episode_gateway import EpisodeUnixTransport
from safe_agent.inference_service import episode_service
from safe_agent.inference_service.providers.local_openai_episode import (
    LocalOpenAIEpisodeBackend,
    local_openai_backend_capabilities,
)
from safe_agent.orchestration.episode_runtime import EpisodeRuntime
from safe_agent.storage.episode_runtime import RuntimeStorageDenied
from tests.episodes.test_episode_foundation import NOW
from tests.episodes.test_episode_runtime_audit import runtime_fixture

BASE = "http://127.0.0.1:11434/v1"
MODEL = "authored-local:3b"
TIMING = LocalTimingProfile(provider_timeout_seconds=90.0,
                           transport_timeout_seconds=105.0, request_timeout_seconds=115.0)


def config(**changes):
    return BenchmarkConfig(**{
        "model_route": "local", "local_api_base": BASE, "local_timing": TIMING,
        "llm_agent": MODEL, "llm_user": MODEL, "agent_proposal_format": "native_tools_v1",
        **changes,
    })


def test_default_config_and_route_keep_legacy_serialization():
    assert "agent_proposal_format" not in BenchmarkConfig().model_dump(mode="json")
    assert "proposal_format" not in EpisodeBackendRoute().model_dump(mode="json")
    assert config().model_dump(mode="json")["agent_proposal_format"] == "native_tools_v1"


@pytest.mark.parametrize("changes", [
    {"model_route": "openai", "local_api_base": None, "local_timing": None},
    {"system": "llm_agent"},
    {"runtime_factory": "authored.fixture:factory"},
    {"agent_proposal_format": "native_tools_v2"},
])
def test_native_proposals_cannot_be_silently_ignored_by_another_route(changes):
    with pytest.raises(ValueError):
        config(**changes)


def test_agent_presentation_does_not_modify_official_customer_settings():
    native = config()
    legacy = config(agent_proposal_format="candidate_json_v1")
    assert harness_model_settings(native, "user") == harness_model_settings(legacy, "user")
    assert harness_model_settings(native, "agent") == harness_model_settings(legacy, "agent")
    assert native.llm_args_user == legacy.llm_args_user


def test_qualification_condition_binds_proposal_format():
    legacy = qualification.qualification_condition(config(agent_proposal_format="candidate_json_v1"))
    native = qualification.qualification_condition(config())
    assert native.pop("agent_proposal_format") == "native_tools_v1"
    assert legacy.pop("agent_proposal_format") == "candidate_json_v1"
    assert native == legacy


def test_native_capability_identity_differs_and_rejects_mislabelled_format():
    legacy = local_openai_backend_capabilities(model=MODEL, base_url=BASE, local_timing=TIMING)
    native = local_openai_backend_capabilities(
        model=MODEL, base_url=BASE, local_timing=TIMING, proposal_format="native_tools_v1",
    )
    assert native.profile == "local_openai_tools_v1" and native.response_format == "native_tools"
    assert native.system_instruction_hash != legacy.system_instruction_hash
    assert native.candidate_schema_hash == legacy.candidate_schema_hash
    assert native.local_timing == legacy.local_timing == TIMING
    for values in (
        native.model_dump() | {"response_format": "json_object"},
        native.model_dump() | {"profile": "openai_chat_v1"},
        legacy.model_dump() | {"response_format": "native_tools"},
    ):
        with pytest.raises(ValueError):
            EpisodeBackendCapabilities.model_validate(values)


def test_native_environment_selection_never_reads_a_hosted_key(monkeypatch):
    monkeypatch.setenv("SAFE_EPISODE_BACKEND", "local_openai")
    monkeypatch.setenv("SAFE_EPISODE_LOCAL_BASE_URL", BASE)
    monkeypatch.setenv("SAFE_EPISODE_LOCAL_TIMING_JSON", TIMING.model_dump_json())
    monkeypatch.setenv("SAFE_EPISODE_PROPOSAL_FORMAT", "native_tools_v1")
    monkeypatch.setenv("SAFE_EPISODE_OPENAI_KEY_FILE", "/must-not-open")
    monkeypatch.setattr(episode_service, "_secret_from_file", lambda _: pytest.fail("Hosted key read"))
    backend = episode_service.backend_from_environment(
        model=MODEL, dispatch_key=SecretStr("authored-dispatch-" + "a" * 40),
    )
    try:
        assert isinstance(backend, LocalOpenAIEpisodeBackend)
        assert backend.capabilities.profile == "local_openai_tools_v1"
    finally:
        backend.close()
    monkeypatch.setenv("SAFE_EPISODE_BACKEND", "hosted_openai")
    monkeypatch.delenv("SAFE_EPISODE_LOCAL_BASE_URL")
    monkeypatch.delenv("SAFE_EPISODE_LOCAL_TIMING_JSON")
    with pytest.raises(ValueError, match="HOSTED_MODEL_LOCAL_API_BASE_DENIED"):
        episode_service.backend_from_environment(
            model=MODEL, dispatch_key=SecretStr("authored-dispatch-" + "a" * 40),
        )


def test_preflight_requires_native_capabilities_without_inference(monkeypatch, tmp_path):
    from safe_agent.inference_service import __main__ as service_main

    key = tmp_path / "dispatch-key"
    monkeypatch.setenv("SAFE_EPISODE_SOCKET", str(tmp_path / "episode.sock"))
    monkeypatch.setenv("SAFE_EPISODE_DISPATCH_KEY_FILE", str(key))
    monkeypatch.setattr(service_main, "_secret_from_file", lambda _: SecretStr("a" * 48))
    observed = []

    def capture(self, authenticator, *, expected, absolute_timeout_seconds):
        observed.append(expected)
        assert absolute_timeout_seconds == 5.0
        raise ValueError("AUTHORED_CAPABILITY_MISMATCH")

    monkeypatch.setattr(EpisodeUnixTransport, "capabilities", capture)
    with pytest.raises(ValueError, match="AUTHORED_CAPABILITY_MISMATCH"):
        preflight._probe_service(config())
    assert observed == [EpisodeServiceCapabilities(backend=local_openai_backend_capabilities(
        model=MODEL, base_url=BASE, local_timing=TIMING, proposal_format="native_tools_v1",
    ))]


def test_durable_run_cannot_resume_with_changed_proposal_interface(tmp_path):
    runtime, transport, context, _, engine = runtime_fixture(tmp_path)
    try:
        EpisodeRuntime(context, engine=engine, transport=transport, clock=lambda: NOW)
        native_context = context.model_copy(update={"agent_proposal_format": "native_tools_v1"})
        with pytest.raises(RuntimeStorageDenied, match="RUN_CONFIGURATION_CHANGED"):
            EpisodeRuntime(native_context, engine=engine, transport=transport, clock=lambda: NOW)
        assert runtime.context.agent_proposal_format == "candidate_json_v1"
    finally:
        engine.dispose()


def test_saved_route_reports_native_proposals_without_upgrading_qualification():
    manifest = {"config": config().model_dump(mode="json")}
    original = deepcopy(manifest)
    route = reporting._route_provenance(manifest)
    assert route["agent_proposal_format"] == "native_tools_v1"
    assert route["model_weights_or_hardware_attested"] is False
    assert reporting._launch_classification(manifest) == "legacy_unqualified"
    assert manifest == original
    manifest["config"]["system"] = "llm_agent"
    with pytest.raises(ValueError, match="RECORDED_AGENT_PROPOSAL_FORMAT_INVALID"):
        reporting._route_provenance(manifest)
