"""Opt-in proposal presentation is a bound condition, never a customer or gate bypass."""

from copy import deepcopy

import pytest
from pydantic import SecretStr

from safe_agent.benchmarks.tau3 import preflight, qualification, reporting
from safe_agent.benchmarks.tau3.config import BenchmarkConfig, harness_model_settings
from safe_agent.contracts.episode_inference import (
    EpisodeBackendCapabilities,
    EpisodeBackendRoute,
    EpisodeCapabilitiesReply,
    EpisodeCapabilitiesRequestEnvelope,
    EpisodeDispatchAuthenticator,
    EpisodeServiceCapabilities,
    LocalTimingProfile,
    wire_digest,
)
from safe_agent.inference.episode_gateway import EpisodeInferenceDenied, EpisodeUnixTransport
from safe_agent.inference_service import episode_service
from safe_agent.inference_service.providers import native_episode
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


@pytest.fixture(params=["native_tools_v1", "native_tools_v2"])
def proposal_format(request):
    return request.param


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
    {"agent_proposal_format": "native_tools_v3"},
])
def test_native_proposals_cannot_be_silently_ignored_by_another_route(changes, proposal_format):
    with pytest.raises(ValueError):
        config(**{"agent_proposal_format": proposal_format, **changes})


def test_agent_presentation_does_not_modify_official_customer_settings(proposal_format):
    native = config(agent_proposal_format=proposal_format)
    legacy = config(agent_proposal_format="candidate_json_v1")
    assert harness_model_settings(native, "user") == harness_model_settings(legacy, "user")
    assert harness_model_settings(native, "agent") == harness_model_settings(legacy, "agent")
    assert native.llm_args_user == legacy.llm_args_user


def test_qualification_condition_binds_proposal_format(proposal_format):
    legacy = qualification.qualification_condition(config(agent_proposal_format="candidate_json_v1"))
    native = qualification.qualification_condition(config(agent_proposal_format=proposal_format))
    assert native.pop("agent_proposal_format") == proposal_format
    assert legacy.pop("agent_proposal_format") == "candidate_json_v1"
    assert native == legacy


def test_native_capability_identity_differs_and_rejects_mislabelled_format(proposal_format):
    legacy = local_openai_backend_capabilities(model=MODEL, base_url=BASE, local_timing=TIMING)
    native = local_openai_backend_capabilities(
        model=MODEL, base_url=BASE, local_timing=TIMING, proposal_format=proposal_format,
    )
    assert native.profile == proposal_format.replace("native_tools", "local_openai_tools")
    assert native.response_format == "native_tools"
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


def test_message_function_identity_is_required_only_for_v2():
    for selected in ("candidate_json_v1", "native_tools_v1", "native_tools_v2"):
        capabilities = local_openai_backend_capabilities(model=MODEL, base_url=BASE, proposal_format=selected)
        values = capabilities.model_dump(mode="json")
        if selected == "native_tools_v2":
            assert values["message_function_hash"] == wire_digest(native_episode.message_function())
            values.pop("message_function_hash")
        else:
            assert "message_function_hash" not in values
            values["message_function_hash"] = wire_digest(native_episode.message_function())
        with pytest.raises(ValueError, match="NATIVE_MESSAGE_FUNCTION_CAPABILITY_MISMATCH"):
            EpisodeBackendCapabilities.model_validate(values)


def test_native_environment_selection_never_reads_a_hosted_key(monkeypatch, proposal_format):
    monkeypatch.setenv("SAFE_EPISODE_BACKEND", "local_openai")
    monkeypatch.setenv("SAFE_EPISODE_LOCAL_BASE_URL", BASE)
    monkeypatch.setenv("SAFE_EPISODE_LOCAL_TIMING_JSON", TIMING.model_dump_json())
    monkeypatch.setenv("SAFE_EPISODE_PROPOSAL_FORMAT", proposal_format)
    monkeypatch.setenv("SAFE_EPISODE_OPENAI_KEY_FILE", "/must-not-open")
    monkeypatch.setattr(episode_service, "_secret_from_file", lambda _: pytest.fail("Hosted key read"))
    backend = episode_service.backend_from_environment(
        model=MODEL, dispatch_key=SecretStr("authored-dispatch-" + "a" * 40),
    )
    try:
        assert isinstance(backend, LocalOpenAIEpisodeBackend)
        assert backend.capabilities.profile == proposal_format.replace("native_tools", "local_openai_tools")
    finally:
        backend.close()
    monkeypatch.setenv("SAFE_EPISODE_BACKEND", "hosted_openai")
    monkeypatch.delenv("SAFE_EPISODE_LOCAL_BASE_URL")
    monkeypatch.delenv("SAFE_EPISODE_LOCAL_TIMING_JSON")
    with pytest.raises(ValueError, match="HOSTED_MODEL_LOCAL_API_BASE_DENIED"):
        episode_service.backend_from_environment(
            model=MODEL, dispatch_key=SecretStr("authored-dispatch-" + "a" * 40),
        )


def test_preflight_requires_native_capabilities_without_inference(monkeypatch, tmp_path, proposal_format):
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
        preflight._probe_service(config(agent_proposal_format=proposal_format))
    assert observed == [EpisodeServiceCapabilities(backend=local_openai_backend_capabilities(
        model=MODEL, base_url=BASE, local_timing=TIMING, proposal_format=proposal_format,
    ))]


def test_durable_run_cannot_resume_with_changed_proposal_interface(tmp_path, proposal_format):
    runtime, transport, context, _, engine = runtime_fixture(tmp_path)
    try:
        EpisodeRuntime(context, engine=engine, transport=transport, clock=lambda: NOW)
        native_context = context.model_copy(update={"agent_proposal_format": proposal_format})
        with pytest.raises(RuntimeStorageDenied, match="RUN_CONFIGURATION_CHANGED"):
            EpisodeRuntime(native_context, engine=engine, transport=transport, clock=lambda: NOW)
        assert runtime.context.agent_proposal_format == "candidate_json_v1"
    finally:
        engine.dispose()


def test_saved_route_reports_native_proposals_without_upgrading_qualification(proposal_format):
    manifest = {"config": config(agent_proposal_format=proposal_format).model_dump(mode="json")}
    original = deepcopy(manifest)
    route = reporting._route_provenance(manifest)
    assert route["agent_proposal_format"] == proposal_format
    assert route["model_weights_or_hardware_attested"] is False
    assert reporting._launch_classification(manifest) == "legacy_unqualified"
    assert manifest == original
    manifest["config"]["system"] = "llm_agent"
    with pytest.raises(ValueError, match="RECORDED_AGENT_PROPOSAL_FORMAT_INVALID"):
        reporting._route_provenance(manifest)


@pytest.mark.parametrize("original,changed", [
    ("native_tools_v1", "native_tools_v2"), ("native_tools_v2", "native_tools_v1"),
])
def test_native_versions_cannot_resume_each_others_durable_run(tmp_path, original, changed):
    _, transport, context, _, engine = runtime_fixture(tmp_path)
    first = context.model_copy(update={"run_id": "native_version_binding", "agent_proposal_format": original})
    try:
        runtime = EpisodeRuntime(first, engine=engine, transport=transport, clock=lambda: NOW)
        with pytest.raises(RuntimeStorageDenied, match="RUN_CONFIGURATION_CHANGED"):
            EpisodeRuntime(first.model_copy(update={"agent_proposal_format": changed}),
                           engine=engine, transport=transport, clock=lambda: NOW)
        assert runtime.context.agent_proposal_format == original
        assert transport.requests == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("expected_format,actual_format", [
    ("native_tools_v1", "native_tools_v2"), ("native_tools_v2", "native_tools_v1"),
    ("native_tools_v2", "native_tools_v2"),
])
def test_signed_sidecar_reply_with_other_native_identity_is_rejected_before_inference(
    tmp_path, monkeypatch, expected_format, actual_format,
):
    auth = EpisodeDispatchAuthenticator(SecretStr("authored-dispatch-" + "a" * 40))
    expected = EpisodeServiceCapabilities(backend=local_openai_backend_capabilities(
        model=MODEL, base_url=BASE, local_timing=TIMING, proposal_format=expected_format,
    ))
    if expected_format == actual_format:
        original = native_episode.message_function

        def different_description():
            function = original()
            function["function"]["description"] = "Authored drift in the synthetic function."
            return function

        monkeypatch.setattr(native_episode, "message_function", different_description)
    actual = EpisodeServiceCapabilities(backend=local_openai_backend_capabilities(
        model=MODEL, base_url=BASE, local_timing=TIMING, proposal_format=actual_format,
    ))
    assert expected.backend.candidate_schema_hash == actual.backend.candidate_schema_hash
    if expected_format == actual_format:
        assert expected.backend.profile == actual.backend.profile
        assert expected.backend.system_instruction_hash == actual.backend.system_instruction_hash
        assert expected.backend.message_function_hash != actual.backend.message_function_hash
    else:
        assert expected.backend.profile != actual.backend.profile
        assert expected.backend.system_instruction_hash != actual.backend.system_instruction_hash
    transport = EpisodeUnixTransport(tmp_path / "unused.sock")
    routes = []

    def answer(route, body, *, max_reply_bytes):
        routes.append(route)
        assert route == "/v1/episode/capabilities" and max_reply_bytes == 16_384
        challenge = auth.verify_capabilities_request(
            EpisodeCapabilitiesRequestEnvelope.model_validate_json(body),
        )
        reply = EpisodeCapabilitiesReply(
            nonce=challenge.nonce, request_hash=challenge.digest, issued_at=NOW,
            deadline=challenge.deadline, capabilities=actual,
        )
        return auth.sign_capabilities_reply(reply).model_dump_json().encode()

    monkeypatch.setattr(transport, "_post", answer)
    with pytest.raises(EpisodeInferenceDenied, match="^EPISODE_CAPABILITIES_VERIFICATION_FAILED$"):
        transport.capabilities(auth, expected=expected, clock=lambda: NOW)
    assert routes == ["/v1/episode/capabilities"]
