"""Authored durable journals: aggregates, uncertainty, review and no private export."""

import hashlib
import json
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from safe_agent.benchmarks.tau3.__main__ import main
from safe_agent.benchmarks.tau3.diagnostics import (
    DiagnosticsError,
    diagnose_database,
    report_diagnostics,
)
from safe_agent.canonical import canonical_json, sha256
from safe_agent.contracts.episode_inference import EpisodeInferenceReply, EpisodeInferenceRequest
from safe_agent.contracts.episodes import (
    AuthorizationBinding,
    BudgetDelta,
    EpisodeEventKind,
    EpisodeLimits,
    EpisodeSpec,
    EpisodeState,
    OutgoingCall,
    RunSpec,
    arguments_digest,
    canonical_arguments_json,
)
from safe_agent.evidence.episodes import replay_episode, seal_episode_event
from safe_agent.storage.episode_runtime import (
    EpisodeModelAttemptRow,
    EpisodePrivateContextRow,
    EpisodeRuntimeStore,
)
from safe_agent.storage.episodes import EpisodeStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PRIVATE = "PRIVATE_CUSTOMER_CANARY_9417"
LIMITS = EpisodeLimits(
    max_turns=100,
    max_model_calls=100,
    max_tool_calls=100,
    max_tokens=100_000,
    max_cost_microusd=1_000_000,
)


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def add_episode(store, private, name, *, run_id="run_authored", diagnostics=None):
    store.create_run(
        RunSpec(
            run_id=run_id,
            organization_id="authored_organization",
            policy_hash=sha256("run"),
            limits=LIMITS,
            created_at=NOW,
            deadline=at(3600),
        )
    )
    store.create_episode(
        EpisodeSpec(
            episode_id=name,
            run_id=run_id,
            actor_id="authored_customer",
            domain="retail",
            policy_hash=sha256("policy"),
            limits=LIMITS,
            created_at=NOW,
            deadline=at(1800),
        )
    )
    private.create(
        name,
        {
            "episode_id": name,
            "diagnostics": diagnostics or [],
            "history": [{"role": "user", "content": PRIVATE}],
            "seen_observations": [],
        },
    )


def model_attempt(
    store, private, episode, request_id, start, finish, *, invalid=False, journal_finish=True
):
    view = store.inspect(episode)
    request = EpisodeInferenceRequest(
        episode_id=episode,
        request_id=request_id,
        domain="retail",
        model="authored-model",
        policy_hash=view.policy_hash,
        observation_hash=view.observation_hash,
        projected_context={"private_canary": PRIVATE},
        max_output_tokens=64,
        issued_at=at(start),
        deadline=at(start + 60),
    )
    store.reserve_inference(
        episode,
        request_id,
        request.digest,
        BudgetDelta(model_calls=1, tokens=100, cost_microusd=200),
        at(start),
    )
    private.prepare_inference(request)
    private.claim_inference(request)
    if finish is None:
        private.uncertain(request_id)
        store.mark_inference_uncertain(episode, request_id, sha256("timeout"), at(start + 3))
    else:
        output = (
            PRIVATE
            if invalid
            else json.dumps(
                {
                    "kind": "message",
                    "purpose": "question",
                    "text": PRIVATE,
                }
            )
        )
        reply = EpisodeInferenceReply(
            request_id=request_id,
            input_hash=request.digest,
            model=request.model,
            output_json=output,
            input_tokens=10,
            output_tokens=5,
            usage_source="scripted",
        )
        private.complete_inference(request, reply)
        if journal_finish:
            store.finish_inference(episode, request_id, reply.digest, at(finish))


def tool_call(store, episode, call_id, start, *, receipt=True, consent=False):
    view = store.inspect(episode)
    args = canonical_arguments_json({"private_argument": PRIVATE})
    call = OutgoingCall(
        call_id=call_id,
        operation="authored_operation",
        arguments_json=args,
        arguments_hash=arguments_digest(args),
        policy_hash=view.policy_hash,
        observation_hash=view.observation_hash,
        consent_hash=sha256("consent") if consent else None,
    )
    auth = AuthorizationBinding(
        episode_id=episode,
        action_hash=call.action_hash,
        policy_hash=call.policy_hash,
        observation_hash=call.observation_hash,
        state_version=view.state_version,
        consent_hash=call.consent_hash,
        domain_admission_hash=sha256("TAL receipt") if receipt else None,
    )
    store.prepare_call(episode, call, auth, view.state_version, at(start))


@pytest.fixture
def authored_journal(tmp_path):
    path = tmp_path / "episodes.sqlite"
    engine = create_engine("sqlite+pysqlite:///" + str(path))
    store = EpisodeStore(engine)
    store.create_schema()
    EpisodePrivateContextRow.__table__.create(engine)
    EpisodeModelAttemptRow.__table__.create(engine)
    private = EpisodeRuntimeStore(engine)
    add_episode(
        store,
        private,
        "ep_complete",
        diagnostics=[
            {"code": "DOMAIN_SUBJECT_MISMATCH", "proposal_hash": sha256("proposal_1")},
            {"code": "DOMAIN_SUBJECT_MISMATCH", "repair": True},
            {"code": "CUSTOMER_CONSENT_REQUIRED", "proposal_hash": sha256("proposal_2")},
            {"code": PRIVATE, "proposal_hash": sha256("proposal_3")},
        ],
    )
    model_attempt(store, private, "ep_complete", "request_1", 1, 3)
    model_attempt(store, private, "ep_complete", "request_2", 4, 9, invalid=True)
    tool_call(store, "ep_complete", "call_1", 10)
    store.consume_result("ep_complete", "call_1", sha256("success"), True, at(14))
    tool_call(store, "ep_complete", "call_2", 15, consent=True)
    store.mark_call_uncertain("ep_complete", "call_2", sha256("timeout"), at(16))
    store.consume_result("ep_complete", "call_2", sha256("failure"), False, at(23))
    store.finish("ep_complete", EpisodeState.FINISHED, at(24))
    add_episode(store, private, "ep_tool_unknown")
    tool_call(store, "ep_tool_unknown", "call_3", 1, receipt=False)
    store.mark_call_uncertain("ep_tool_unknown", "call_3", sha256("timeout"), at(4))
    private.claim("ep_tool_unknown", "observation_active", 0)
    add_episode(store, private, "ep_model_unknown")
    model_attempt(store, private, "ep_model_unknown", "request_3", 1, None)
    yield path, engine, store, private
    engine.dispose()


def test_aggregate_diagnostics_preserve_uncertainty_and_exclude_private_values(authored_journal):
    path, _, _, _ = authored_journal
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = diagnose_database(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert report["integrity"]["passed"]
    assert report["episodes"]["by_state"] == {"FINISHED": 1, "OUTCOME_UNCERTAIN": 2}
    assert report["episodes"]["processing_claims_active"] == 1
    tools = report["tool_calls"]
    assert (tools["prepared"], tools["completed"], tools["without_result"]) == (3, 2, 1)
    assert tools["uncertain_ever"] == 2 and tools["uncertain_unresolved"] == 1
    assert tools["resolved_after_uncertainty"] == 1
    assert tools["successful_results"] == 1 and tools["failed_results"] == 1
    assert tools["actual_external_dispatches"] is None
    auth = report["authorization"]
    assert auth["verified_persisted_bindings"] == 3
    assert auth["bindings_with_domain_admission_receipt"] == 2
    assert auth["bindings_without_domain_admission_receipt"] == 1
    assert auth["bindings_with_customer_consent"] == 1
    assert not auth["semantic_authorization_reverified"]
    assert report["denials"]["domain_proposals"] == 3
    assert report["denials"]["repair_feedback"] == 1
    assert report["denials"]["by_reason"] == {
        "DOMAIN_SUBJECT_MISMATCH": 1,
        "CUSTOMER_CONSENT_REQUIRED": 1,
        "OTHER": 1,
    }
    model = report["model_attempts"]
    assert model["reserved"] == 3 and model["known_replies"] == 2
    assert model["verified_attempts_by_state"] == {"COMPLETE": 2, "UNCERTAIN": 1}
    assert model["schema_rejected_replies"] == 1 and model["schema_accepted_replies"] == 1
    assert model["lifetime_reserved_tokens"] == 300
    assert model["lifetime_reserved_cost_microusd"] == 600
    assert model["actual_billed_cost_usd"] is None
    assert not model["reserved_cost_is_actual_spend"]
    latency = report["latency_seconds"]
    assert latency["inference_reservation_to_finish"]["mean"] == 3.5
    assert latency["inference_reservation_to_finish"]["p50"] == 2.0
    assert latency["tool_prepare_to_result"]["mean"] == 6.0
    assert latency["tool_prepare_to_result"]["p95"] == 8.0
    assert latency["inference_without_endpoint"] == latency["tools_without_endpoint"] == 1
    assert report["false_refusals"]["reviewed_assessable_denial_rate"] is None
    assert report["false_refusals"]["all_recorded_denial_rate"] is None
    serialized = json.dumps(report)
    for private_value in (
        PRIVATE,
        "ep_complete",
        "request_1",
        "call_1",
        "authored_operation",
        "sha256:",
    ):
        assert private_value not in serialized


def review(index, label):
    return {
        "schema_version": "safe_agent.denial_review.v1",
        "episode_id": "ep_complete",
        "diagnostic_index": index,
        "proposal_hash": sha256("proposal_1" if index == 0 else "proposal_2"),
        "reviewed": True,
        "reviewer_id": "private_reviewer",
        "reviewed_at": NOW.isoformat(),
        "label": label,
    }


def test_false_refusal_rate_requires_exact_reviewed_denial_annotations(authored_journal, tmp_path):
    path, _, _, _ = authored_journal
    annotations = tmp_path / "private_reviews.jsonl"
    annotations.write_text(
        "\n".join(
            json.dumps(value)
            for value in (
                review(0, "false_refusal"),
                review(2, "correct_denial"),
            )
        )
    )
    report = diagnose_database(path, annotations=annotations)
    rates = report["false_refusals"]
    assert rates["reviewed"] == 2 and rates["unreviewed"] == 1
    assert rates["reviewed_assessable_denial_rate"] == 0.5
    assert rates["all_recorded_denial_rate"] is None
    assert "private_reviewer" not in json.dumps(report)
    for invalid in (
        [dict(review(0, "false_refusal"), reviewed=False)],
        [dict(review(0, "false_refusal"), proposal_hash=sha256("other"))],
        [review(1, "false_refusal")],  # Repair feedback is not a second denial.
        [review(0, "false_refusal"), review(0, "correct_denial")],
    ):
        annotations.write_text("\n".join(json.dumps(value) for value in invalid))
        with pytest.raises(DiagnosticsError, match="INVALID_REVIEW_ANNOTATIONS"):
            diagnose_database(path, annotations=annotations)


def test_replay_and_private_context_corruption_are_aggregate_failures(authored_journal):
    path, engine, _, private = authored_journal
    private.claim("ep_model_unknown", "authored_claim", 0)
    with engine.begin() as db:
        db.execute(
            text(
                "UPDATE safe_episodes SET state_version = state_version + 1, snapshot_json = :bad "
                "WHERE episode_id = 'ep_complete'"
            ),
            {"bad": json.dumps({"private_corruption": PRIVATE})},
        )
        db.execute(
            text(
                "UPDATE safe_episode_private_contexts SET version = version + 1, "
                "active_observation = NULL, context_hash = :bad WHERE episode_id = 'ep_model_unknown'"
            ),
            {"bad": sha256("incorrect")},
        )
    report = diagnose_database(path)
    assert not report["integrity"]["passed"]
    assert report["integrity"]["episode_replay_failures"] == 1
    assert report["integrity"]["failures_by_check"]["private_context_integrity_invalid"] == 1
    assert report["episodes"]["recorded"] == 3 and report["episodes"]["replay_verified"] == 2
    assert report["tool_calls"]["completed"] == 0
    assert PRIVATE not in json.dumps(report)


def test_read_only_cli_and_scoped_run_do_not_modify_reward_files(
    authored_journal, tmp_path, capsys
):
    path, _, store, private = authored_journal
    add_episode(store, private, "ep_other", run_id="run_other")
    result = diagnose_database(path, run_id="run_other")
    assert result["episodes"]["recorded"] == 1 and result["model_attempts"]["reserved"] == 0
    output = tmp_path / "diagnostics.json"
    assert main(["diagnostics", "--database", str(path), "--output", str(output)]) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert PRIVATE not in capsys.readouterr().out
    official = tmp_path / "results.json"
    official.write_text('{"authored_official_reward":0.25}')
    original = official.read_bytes()
    with pytest.raises(DiagnosticsError, match="BENCHMARK_EVIDENCE"):
        report_diagnostics(path, output=official)
    assert official.read_bytes() == original
    with pytest.raises(DiagnosticsError, match="OUTPUT_IS_SOURCE"):
        report_diagnostics(path, output=path)


def test_missing_database_is_sanitized_and_not_created(tmp_path, capsys):
    missing = tmp_path / (PRIVATE + ".sqlite")
    assert main(["diagnostics", "--database", str(missing)]) == 2
    output = capsys.readouterr().out
    assert "DIAGNOSTICS_DATABASE_NOT_FOUND" in output
    assert PRIVATE not in output
    assert not missing.exists()


def test_partial_commit_boundaries_remain_visible_without_inventing_outcomes(authored_journal):
    path, _, store, private = authored_journal
    add_episode(store, private, "ep_reply_saved")
    model_attempt(store, private, "ep_reply_saved", "request_4", 1, 3, journal_finish=False)
    add_episode(store, private, "ep_reserved_only")
    store.reserve_inference(
        "ep_reserved_only",
        "request_5",
        sha256("reserved but not prepared"),
        BudgetDelta(model_calls=1, tokens=100, cost_microusd=200),
        at(1),
    )
    report = diagnose_database(path)
    assert report["integrity"]["passed"]
    model = report["model_attempts"]
    assert model["reserved"] == 5 and model["journal_finished"] == 2
    assert model["known_replies"] == 3
    assert model["known_replies_without_journal_finish"] == 1
    assert model["reservations_without_attempt_row"] == 1
    assert report["latency_seconds"]["inference_reservation_to_finish"]["samples"] == 2
    assert report["latency_seconds"]["inference_without_endpoint"] == 3


def test_valid_replay_with_missing_run_parent_is_still_an_integrity_failure(authored_journal):
    path, _, _, _ = authored_journal
    spec = EpisodeSpec(
        episode_id="ep_orphan",
        run_id="run_absent",
        actor_id="authored_customer",
        domain="retail",
        policy_hash=sha256("policy"),
        limits=LIMITS,
        created_at=NOW,
        deadline=at(1800),
    )
    event = seal_episode_event(
        episode_id=spec.episode_id,
        kind=EpisodeEventKind.CREATED,
        sequence=1,
        state_version=0,
        now=NOW,
        payload={"spec_json": canonical_json(spec).decode()},
        previous_hash=None,
    )
    view = replay_episode([event])
    # Model an imported/corrupt snapshot using a separate SQLite connection whose
    # foreign-key enforcement is off; normal EpisodeStore cannot create this row.
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO safe_episodes (episode_id, run_id, spec_json, spec_hash, state_version, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                spec.episode_id,
                spec.run_id,
                canonical_json(spec).decode(),
                sha256(spec),
                0,
                view.model_dump_json(),
            ),
        )
        db.execute(
            "INSERT INTO safe_episode_events (episode_id, sequence, kind, event_hash, record_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (spec.episode_id, 1, event.kind.value, event.event_hash, event.model_dump_json()),
        )
    report = diagnose_database(path)
    assert report["integrity"]["episode_replay_failures"] == 0
    assert report["integrity"]["failures_by_check"]["episode_run_missing"] == 1
    assert not report["integrity"]["passed"]
