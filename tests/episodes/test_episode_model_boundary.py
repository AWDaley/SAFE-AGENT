"""Model boundary acceptance without provider network calls or credentials."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from safe_agent.canonical import sha256
from safe_agent.contracts.episode_inference import (
    EpisodeDispatchAuthenticator,
    EpisodeInferenceReply,
    EpisodeInferenceRequest,
    MessageCandidate,
    parse_candidate,
    strict_json,
)
from safe_agent.contracts.episodes import BudgetDelta, EpisodeState
from safe_agent.inference.episode_gateway import (
    EpisodeInferenceDenied,
    EpisodeInferenceGateway,
    EpisodeUnixTransport,
    ScriptedEpisodeTransport,
)
from safe_agent.inference_service.episode_service import DurableServiceFence, create_episode_service
from safe_agent.inference_service.providers.openai_episode import OpenAIEpisodeBackend
from safe_agent.storage.episode_runtime import (
    EpisodeModelAttemptRow,
    EpisodePrivateContextRow,
    EpisodeRuntimeStore,
    RuntimeStorageDenied,
)
from safe_agent.storage.episodes import EpisodeStore
from tests.episodes.test_episode_foundation import NOW, seed

CANARY = "SYNTHETIC_PRIVATE_PROVIDER_ERROR_9273"
ANSWER = MessageCandidate(kind="message", purpose="answer", text="How can I help?")


class InProcessClient:
    """Exercise the real ASGI route without TestClient's cross-thread portal."""

    def __init__(self, app, *, base_url):
        self.app = app
        self.base_url = base_url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url=self.base_url) as client:
                return await client.post(url, **kwargs)
        return asyncio.run(send())


@pytest.fixture
def boundary(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'boundary.sqlite'}")
    episodes = EpisodeStore(engine)
    episodes.create_schema()
    EpisodePrivateContextRow.__table__.create(engine)
    EpisodeModelAttemptRow.__table__.create(engine)
    _, _, view = seed(episodes)
    journal = EpisodeRuntimeStore(engine)
    auth = EpisodeDispatchAuthenticator(SecretStr("dispatch-key-" + "a" * 40))
    yield episodes, journal, auth, view
    engine.dispose()


def request(view, request_id="request_1", **changes):
    values = {
        "request_id": request_id, "episode_id": view.episode_id, "domain": view.domain,
        "model": "offline_model", "policy_hash": view.policy_hash,
        "observation_hash": view.observation_hash, "projected_context": {"question": "Hello"},
        "max_output_tokens": 128, "issued_at": NOW, "deadline": NOW + timedelta(seconds=60),
    }
    return EpisodeInferenceRequest(**{**values, **changes})


def reply(req, **changes):
    values = {
        "request_id": req.request_id, "input_hash": req.digest, "model": req.model,
        "output_json": ANSWER.model_dump_json(), "input_tokens": 10, "output_tokens": 10,
        "usage_source": "scripted",
    }
    return EpisodeInferenceReply(**{**values, **changes})


def gateway(boundary, transport, *, clock=lambda: NOW):
    episodes, journal, auth, _ = boundary
    return EpisodeInferenceGateway(
        episodes=episodes, journal=journal, authenticator=auth, transport=transport, clock=clock
    )


def test_exact_request_reply_are_durable_and_restart_never_redispatches(boundary):
    episodes, journal, _, view = boundary
    transport = ScriptedEpisodeTransport([ANSWER])
    req = request(view)
    candidate, result = gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=10)
    assert candidate == ANSWER
    recovered = EpisodeRuntimeStore(episodes.engine)
    assert recovered.inference(req.request_id) == (req, result, "COMPLETE")
    assert episodes.get_reservation(view.episode_id, req.request_id).result_hash == result.digest
    assert episodes.inspect(view.episode_id).state is EpisodeState.READY
    with pytest.raises(EpisodeInferenceDenied, match="PREPARATION_DENIED"):
        gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=10)
    assert len(transport.requests) == 1
    assert episodes.get_run(view.run_id).usage == BudgetDelta(model_calls=1, tokens=200,
                                                           cost_microusd=10)
    with pytest.raises(RuntimeStorageDenied, match="RESULT_REPLAY"):
        journal.complete_inference(req, result)


def test_crash_after_claim_retains_fence_and_hold_on_restart(boundary):
    episodes, journal, _, view = boundary
    req = request(view)
    episodes.reserve_inference(view.episode_id, req.request_id, req.digest,
                               BudgetDelta(model_calls=1, tokens=200), NOW)
    journal.prepare_inference(req)
    journal.claim_inference(req)
    transport = ScriptedEpisodeTransport([ANSWER])
    with pytest.raises(EpisodeInferenceDenied, match="PREPARATION_DENIED"):
        gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=0)
    assert not transport.requests
    assert journal.inference(req.request_id)[2] == "DISPATCHING"
    assert episodes.inspect(view.episode_id).state is EpisodeState.INFERENCE_PENDING
    assert episodes.get_run(view.run_id).usage.tokens == 200


@pytest.mark.parametrize("changes", [
    {"request_id": "wrong_request"}, {"input_hash": sha256("wrong input")},
    {"model": "wrong_model"}, {"output_tokens": 129}, {"input_tokens": 201},
])
def test_mismatched_or_over_allowance_replies_are_uncertain_without_second_attempt(boundary, changes):
    episodes, journal, _, view = boundary
    req = request(view)
    transport = ScriptedEpisodeTransport([])
    transport.infer = lambda envelope: reply(envelope.request, **changes)
    with pytest.raises(EpisodeInferenceDenied, match="OUTCOME_UNCERTAIN"):
        gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=20)
    assert journal.inference(req.request_id) == (req, None, "UNCERTAIN")
    assert episodes.inspect(view.episode_id).state is EpisodeState.OUTCOME_UNCERTAIN
    assert episodes.get_reservation(view.episode_id, req.request_id).uncertain
    assert episodes.get_run(view.run_id).usage.cost_microusd == 20


def test_provider_error_is_sanitized_and_uncertain_hold_is_retained(boundary):
    episodes, journal, _, view = boundary

    def fail(_):
        raise TimeoutError(CANARY)

    transport = ScriptedEpisodeTransport(fail)
    req = request(view)
    with pytest.raises(EpisodeInferenceDenied) as raised:
        gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=50)
    assert str(raised.value) == "EPISODE_INFERENCE_OUTCOME_UNCERTAIN"
    assert raised.value.__cause__ is None
    assert CANARY not in str(episodes.events(view.episode_id))
    assert journal.inference(req.request_id)[2] == "UNCERTAIN"
    with pytest.raises(EpisodeInferenceDenied):
        gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=50)
    assert len(transport.requests) == 1


def test_schema_repair_requires_distinct_id_and_separate_budget(boundary):
    episodes, journal, _, view = boundary
    transport = ScriptedEpisodeTransport(['{"kind":"tool","extra":true}', ANSWER])
    req = request(view)
    with pytest.raises(EpisodeInferenceDenied, match="SCHEMA_REJECTED"):
        gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=10)
    assert journal.inference(req.request_id)[2] == "COMPLETE"
    assert episodes.inspect(view.episode_id).state is EpisodeState.READY
    with pytest.raises(EpisodeInferenceDenied, match="PREPARATION_DENIED"):
        gateway(boundary, transport).infer(req, token_hold=200, cost_hold_microusd=10)
    repaired = request(episodes.inspect(view.episode_id), "request_repair",
                       projected_context={"feedback": "EPISODE_INFERENCE_SCHEMA_REJECTED"})
    candidate, _ = gateway(boundary, transport).infer(repaired, token_hold=200, cost_hold_microusd=10)
    assert candidate == ANSWER and len(transport.requests) == 2
    assert episodes.get_run(view.run_id).usage == BudgetDelta(model_calls=2, tokens=400,
                                                           cost_microusd=20)


@pytest.mark.parametrize("changes", [
    {"policy_hash": sha256("wrong policy")}, {"observation_hash": sha256("stale")},
    {"domain": "banking_knowledge"}, {"deadline": NOW + timedelta(seconds=121)},
])
def test_source_fields_are_bound_before_model_admission(boundary, changes):
    episodes, _, _, view = boundary
    transport = ScriptedEpisodeTransport([ANSWER])
    with pytest.raises(EpisodeInferenceDenied, match="PREPARATION_DENIED"):
        gateway(boundary, transport).infer(request(view, **changes), token_hold=200,
                                           cost_hold_microusd=0)
    assert not transport.requests and episodes.get_run(view.run_id).usage.model_calls == 0


def test_context_claim_is_durable_and_saved_observation_cannot_replay(boundary):
    episodes, journal, _, view = boundary
    journal.create(view.episode_id, {"private": CANARY})
    initial = journal.load(view.episode_id)
    with pytest.raises(RuntimeStorageDenied, match="REQUIRES_PROCESSING_CLAIM"):
        journal.save(view.episode_id, initial, initial.data)
    claimed = journal.claim(view.episode_id, "observation_1", initial.version)
    restarted = EpisodeRuntimeStore(episodes.engine)
    with pytest.raises(RuntimeStorageDenied, match="PROCESSING_OR_STALE"):
        restarted.claim(view.episode_id, "observation_2", claimed.version)
    restarted.save(view.episode_id, claimed, {"private": CANARY})
    saved = restarted.load(view.episode_id)
    assert saved.data["seen_observations"] == ["observation_1"]
    second = restarted.claim(view.episode_id, "observation_2", saved.version)
    restarted.save(view.episode_id, second, {"private": CANARY})
    saved = restarted.load(view.episode_id)
    assert saved.data["seen_observations"] == ["observation_1", "observation_2"]
    with pytest.raises(RuntimeStorageDenied, match="ALREADY_CONSUMED"):
        restarted.claim(view.episode_id, "observation_1", saved.version)


def test_completed_inference_scope_and_reply_are_database_immutable(boundary):
    episodes, journal, _, view = boundary
    req = request(view)
    gateway(boundary, ScriptedEpisodeTransport([ANSWER])).infer(
        req, token_hold=200, cost_hold_microusd=0)
    for assignment in (
        "episode_id='other_episode'", "request_id='different_request'",
        "request_json='{}'", "input_hash='forged'", "status='DISPATCHING'",
        "reply_json='{}'", "reply_hash='forged'",
    ):
        with pytest.raises(IntegrityError), episodes.engine.begin() as db:
            db.execute(text("UPDATE safe_episode_model_attempts SET " + assignment))
    with pytest.raises(IntegrityError), episodes.engine.begin() as db:
        db.execute(text("DELETE FROM safe_episode_model_attempts"))
    assert journal.inference(req.request_id)[2] == "COMPLETE"


def test_inference_database_rejects_skips_mismatched_replies_and_uncertain_reset(boundary):
    episodes, journal, _, view = boundary
    req = request(view)
    journal.prepare_inference(req)
    valid = reply(req)
    with pytest.raises(IntegrityError), episodes.engine.begin() as db:
        db.execute(text("UPDATE safe_episode_model_attempts SET status='COMPLETE', "
                        "reply_json=:body, reply_hash=:digest"),
                   {"body": valid.model_dump_json(), "digest": valid.digest})
    journal.claim_inference(req)
    for changed in ({"request_id": "wrong_id"}, {"input_hash": sha256("other input")},
                    {"model": "other_model"}):
        wrong = reply(req, **changed)
        with pytest.raises(IntegrityError), episodes.engine.begin() as db:
            db.execute(text("UPDATE safe_episode_model_attempts SET status='COMPLETE', "
                            "reply_json=:body, reply_hash=:digest"),
                       {"body": wrong.model_dump_json(), "digest": wrong.digest})
    with pytest.raises(IntegrityError), episodes.engine.begin() as db:
        db.execute(text("UPDATE safe_episode_model_attempts SET status='COMPLETE'"))
    journal.uncertain(req.request_id)
    for state in ("PREPARED", "DISPATCHING", "COMPLETE"):
        with pytest.raises(IntegrityError), episodes.engine.begin() as db:
            db.execute(text("UPDATE safe_episode_model_attempts SET status=:state"), {"state": state})
    assert journal.inference(req.request_id) == (req, None, "UNCERTAIN")


def test_inference_insert_cannot_start_dispatched_or_cross_bind_request_json(boundary):
    episodes, _, _, view = boundary
    req = request(view)
    values = {"request_id": req.request_id, "episode_id": req.episode_id,
              "request_json": req.model_dump_json(), "input_hash": req.digest,
              "status": "PREPARED"}
    for change in ({"status": "DISPATCHING"}, {"episode_id": "wrong_episode"},
                   {"request_id": "wrong_request"}, {"request_json": "{}"}):
        with pytest.raises(IntegrityError), episodes.engine.begin() as db:
            db.execute(EpisodeModelAttemptRow.__table__.insert().values(**{**values, **change}))


def test_recovery_of_invalid_typed_source_data_is_sanitized(boundary):
    episodes, journal, _, view = boundary
    req = request(view)
    malformed = {**req.model_dump(mode="json"), "unknown_private_field": CANARY}
    # The SQL guard freezes correlated bytes; typed parsing remains a separate
    # boundary and must not expose a corrupted row's private validation input.
    with episodes.engine.begin() as db:
        db.execute(EpisodeModelAttemptRow.__table__.insert().values(
            request_id=req.request_id, episode_id=req.episode_id,
            request_json=json.dumps(malformed), input_hash=req.digest, status="PREPARED",
        ))
    with pytest.raises(RuntimeStorageDenied) as raised:
        journal.inference(req.request_id)
    assert str(raised.value) == "INFERENCE_STORAGE_INTEGRITY_FAILED"
    assert raised.value.__cause__ is None


def test_context_database_guards_identity_claim_shape_versions_and_history(boundary):
    episodes, journal, _, view = boundary
    journal.create(view.episode_id, {"private": CANARY})
    for assignment in (
        "version=5, active_observation='claim'",
        "episode_id='other_episode', version=version+1, active_observation='claim'",
        "version=version+1",  # A version bump alone cannot bypass the claim/save lifecycle.
        "version=version+1, active_observation='claim', context_json='{}'",
    ):
        with pytest.raises(IntegrityError), episodes.engine.begin() as db:
            db.execute(text("UPDATE safe_episode_private_contexts SET " + assignment))
    claimed = journal.claim(view.episode_id, "observation_1", 0)
    with pytest.raises(IntegrityError), episodes.engine.begin() as db:
        db.execute(text("UPDATE safe_episode_private_contexts "
                        "SET version=version+1, active_observation='replacement'"))
    journal.save(view.episode_id, claimed, {"private": CANARY})
    with pytest.raises(IntegrityError), episodes.engine.begin() as db:
        db.execute(text("DELETE FROM safe_episode_private_contexts"))


class Backend:
    def __init__(self, *, fail=False, mismatch=False):
        self.calls = []
        self.fail = fail
        self.mismatch = mismatch

    def infer(self, req):
        self.calls.append(req)
        if self.fail:
            raise TimeoutError(CANARY)
        return reply(req, model="wrong_model") if self.mismatch else reply(req)


def service(auth, backend, fence):
    return create_episode_service(model="offline_model", authenticator=auth, backend=backend,
                                  fence=fence, clock=lambda: NOW)


def post(client, envelope):
    return client.post("/v1/episode/infer", content=envelope.model_dump_json(),
                       headers={"Content-Type": "application/json"})


def test_sidecar_authentication_and_restart_replay_fence(boundary, tmp_path):
    _, _, auth, view = boundary
    req = request(view)
    backend = Backend()
    path = tmp_path / "fence.sqlite"
    bad = EpisodeDispatchAuthenticator(SecretStr("b" * 40)).sign(req, sha256("reservation"))
    valid = auth.sign(req, sha256("reservation"))
    with InProcessClient(service(auth, backend, DurableServiceFence(path)), base_url="http://safe-episode") as client:
        denied = post(client, bad)
        assert denied.status_code == 403 and denied.content == b""
        assert not backend.calls
        accepted = post(client, valid)
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "no-store"
        assert EpisodeInferenceReply.model_validate_json(accepted.content).request_id == req.request_id
    with InProcessClient(service(auth, backend, DurableServiceFence(path)), base_url="http://safe-episode") as restarted:
        assert post(restarted, valid).status_code == 409
    assert len(backend.calls) == 1


@pytest.mark.parametrize("mode", ["fail", "mismatch"])
def test_sidecar_failures_are_bodyless_and_never_retry_after_restart(boundary, tmp_path, mode):
    _, _, auth, view = boundary
    backend = Backend(**{mode: True})
    path = tmp_path / "failed.sqlite"
    envelope = auth.sign(request(view), sha256("reservation"))
    with InProcessClient(service(auth, backend, DurableServiceFence(path)), base_url="http://safe-episode") as client:
        response = post(client, envelope)
        assert response.status_code == 503 and response.content == b""
        assert CANARY not in str(response.headers)
    with InProcessClient(service(auth, backend, DurableServiceFence(path)), base_url="http://safe-episode") as client:
        assert post(client, envelope).status_code == 409
    assert len(backend.calls) == 1


def test_sidecar_rejects_duplicate_json_keys_before_dispatch(boundary, tmp_path):
    _, _, auth, view = boundary
    backend = Backend()
    envelope = auth.sign(request(view), sha256("reservation"))
    body = envelope.model_dump_json()
    body = '{"signature":"' + envelope.signature + '",' + body[1:]
    with InProcessClient(service(auth, backend, DurableServiceFence(tmp_path / "duplicate.sqlite")),
                    base_url="http://safe-episode") as client:
        response = client.post("/v1/episode/infer", content=body,
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 403
    assert not backend.calls


def test_unix_transport_sanitizes_path_failures(boundary, tmp_path):
    _, _, auth, view = boundary
    transport = EpisodeUnixTransport(tmp_path / CANARY / "missing.sock")
    with pytest.raises(EpisodeInferenceDenied) as raised:
        transport.infer(auth.sign(request(view), sha256("reservation")))
    assert str(raised.value) == "EPISODE_MODEL_TRANSPORT_UNCERTAIN"


@pytest.mark.parametrize("raw", [
    '{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e400}',
])
def test_strict_json_rejects_ambiguous_or_nonfinite_input(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


def test_candidate_schema_cannot_carry_execution_authority():
    with pytest.raises(ValueError):
        parse_candidate('{"kind":"tool","tool_name":"get_order_details",'
                        '"arguments":{},"authorization":{"allowed":true}}')


def test_openai_route_is_fixed_no_retry_or_redirect_and_key_stays_out_of_prompt(boundary, monkeypatch):
    openai = pytest.importorskip("openai", reason="Model SDK is part of the separate tau3 profile")

    _, _, _, view = boundary
    constructor = {}
    submitted = {}
    http_options = {}

    def http_client(**kwargs):
        http_options.update(kwargs)
        return object()

    def create(**kwargs):
        submitted.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=ANSWER.model_dump_json()))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
        )

    def client(**kwargs):
        constructor.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(openai, "OpenAI", client)
    monkeypatch.setattr(openai, "DefaultHttpxClient", http_client)
    backend = OpenAIEpisodeBackend(model="offline_model", api_key=SecretStr(CANARY))
    result = backend.infer(request(view))
    assert constructor["base_url"] == "https://api.openai.com/v1"
    assert constructor["max_retries"] == 0
    assert http_options == {"follow_redirects": False, "trust_env": False}
    assert CANARY not in str(submitted)
    assert result.usage_source == "provider_reported" and result.input_tokens == 12
