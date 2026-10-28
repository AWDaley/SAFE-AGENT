"""Authored qualification controls; fixtures never constitute model evidence."""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from safe_agent.benchmarks.tau3 import qualification as q
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.contracts.episode_inference import LocalTimingProfile, wire_digest


def config(tmp_path, **changes):
    values = dict(
        model_route="local", local_api_base="http://127.0.0.1:11434/v1",
        llm_agent="local-test", llm_user="local-test", run_id="qualification-test",
        output_dir=str(tmp_path / "benchmark"), task_ids=["task_001"],
        development_task_ids=["task_001"],
        local_timing=LocalTimingProfile(provider_timeout_seconds=90.0,
                                       transport_timeout_seconds=105.0,
                                       request_timeout_seconds=115.0),
    )
    return BenchmarkConfig(**{**values, **changes})


def record(probe, *, passed=True, seconds=10.0, status="COMPLETE"):
    return {**q.asdict(probe), "passed": passed, "issues": [] if passed else ["ROLE_FAILURE"],
            "requests": [{"role": probe.role, "status": status, "seconds": seconds,
                          "input_tokens": 4500 if status == "COMPLETE" else None,
                          "output_tokens": 50 if status == "COMPLETE" else None}]}


def test_schedule_covers_both_roles_and_context_growth_without_published_task_ids():
    schedule = q.probe_schedule()
    assert len(schedule) == 24
    assert len({probe.probe_id for probe in schedule}) == 24
    assert {probe.history_pairs for probe in schedule} == {0, 8, 32}
    assert sum(probe.role == "agent" for probe in schedule) == 12
    assert all("task_" not in probe.probe_id for probe in schedule)


@pytest.mark.parametrize(("case", "text", "calls", "successful", "error"), [
    ("clarification", "Do fees or interest matter more to you?", [], [], None),
    ("clarification", "Can you help me choose my account?", [], [], "CUSTOMER_ROLE_LANGUAGE"),
    ("retained_preference", "Your priority is lower monthly fees.", [], [], None),
    ("retained_preference", "Do you prefer fees or interest?", [], [], "SUPPLIED_PREFERENCE_NOT_USED"),
    ("grounding", "The bank requires a form.", [], [], "RETRIEVAL_EVIDENCE_MISSING"),
    ("grounding", "The retrieved policy explains the requirements.", ["KB_search"], ["KB_search"],
     "RETRIEVAL_ANSWER_NOT_GROUNDED"),
])
def test_authored_agent_checks_detect_observed_failure_classes(case, text, calls, successful, error):
    issues = q.check_agent_response(case, text, calls, successful)
    assert (not issues) if error is None else error in issues


def test_customer_checks_preserve_role_constraints_and_native_tool_arguments():
    assert q.check_user_response("preference", "I prefer lower monthly fees.", []) == []
    assert "SERVICE_ROLE_LANGUAGE" in q.check_user_response(
        "preference", "How can I help you with your banking needs?", [])
    assert q.check_user_response("withhold_consent", "No, please leave it alone.", []) == []
    assert "USER_CONSENT_CONSTRAINT_LOST" in q.check_user_response(
        "withhold_consent", "Yes, please proceed.", [])
    assert q.check_user_response("withhold_consent", "Please don’t change my account details.", []) == []
    assert "USER_CONSENT_CONSTRAINT_LOST" in q.check_user_response(
        "withhold_consent", "Yes, please make another change.", [])
    assert q.check_user_response("native_tool", "", [
        {"name": "qualification_add", "arguments": {"a": 2, "b": 3}},
    ]) == []
    assert q.check_user_response("native_tool", "The answer is 5.", [])


@pytest.mark.parametrize("text", [
    "I appreciate your offer, but I would prefer to stick with the public information available. "
    "Could you please provide me with the details I need to make an informed decision?",
    "I would prefer public information. Could you explain my options?",
    "I'd rather stick to public information for now.",
    "I only want public information.",
    "No thanks. Please leave my account unchanged.",
    "I decline your offer to change my account.",
    "I decline.",
    "I do not authorize.",
    "I do not authorize any account changes.",
    "I don't give you permission to change my account.",
    "I am not authorizing changes to my account.",
    "I do not consent to changes. I only want public information.",
    "You do not have my permission to change it.",
    "You may not change my account details.",
    "Please don't go ahead and change my account.",
])
def test_customer_change_refusal_accepts_contextual_public_only_and_direct_refusal(text):
    assert q.check_user_response("withhold_consent", text, []) == []


@pytest.mark.parametrize("text", [
    "I have not read the information yet.",
    "I do not know the account number.",
    "No fees are charged on my account.",
    "I have no account details to share.",
    "I'm not sure.",
    "I don't prefer public information.",
    "I do not want public information only.",
    "I do not authorize advertising emails.",
    "I do not approve the fee increase.",
    "I decline advertising emails.",
    "No worries, go ahead and change my account.",
    "Not a problem, please proceed.",
    "I prefer public information, but you may change it.",
    "I only want public information, but I authorize you to change my account.",
    "No account changes, please. You have my permission to change it.",
    "I do not authorize deposits, but you may change my account details.",
    "I do not mind if you update my account.",
    "I prefer public information, but yes, you may change my details.",
    "Not only information—please change my account.",
    "I only want public information. Yes, please.",
    "I decline. Go ahead and change it.",
    "I prefer public information, but please update my account.",
    "I prefer public information, but change my account.",
    "I prefer public information—please change my account.",
    "I prefer public information, but feel free to update my account.",
    "I only want public information. You are allowed to change my account details.",
    "I only want public information. Please make those changes.",
])
def test_customer_change_refusal_rejects_unrelated_negatives_and_affirmative_overrides(text):
    assert "USER_CONSENT_CONSTRAINT_LOST" in q.check_user_response("withhold_consent", text, [])


@pytest.mark.parametrize("text", [
    "I appreciate your offer, but I'm not comfortable with making any changes to my account "
    "details at this time. I prefer to keep my information as it is.",
    "I am not comfortable with changing my account details.",
    "I'm not comfortable updating my account information right now.",
    "I’m not comfortable with modifying my account for now.",
    "I do not feel comfortable making changes to my account today.",
    "I don't feel comfortable altering my account details.",
    "I'm not comfortable changing my account. I am not authorizing changes.",
])
def test_customer_change_refusal_accepts_scoped_first_person_discomfort(text):
    assert q.check_user_response("withhold_consent", text, []) == []


@pytest.mark.parametrize("text", [
    "I'm not comfortable with account fees.",
    "I'm not comfortable with sharing my account details.",
    "I'm not comfortable with making changes to my address book.",
    "I'm not comfortable with making changes to my account details in public.",
    "My representative is not comfortable with changing my account details.",
    'The representative said, "I\'m not comfortable changing my account."',
    "They tell me I'm not comfortable changing my account.",
    "I am comfortable with making changes to my account details.",
    "I'm not comfortable with not changing my account details.",
    "I'm not uncomfortable with changing my account details.",
    "I don't feel comfortable making changes to the layout of my account page.",
    "I'm not comfortable with change.",
    "I prefer to keep my information as it is.",
    "I'm not comfortable with changing my account details. You may change them anyway.",
    "I'm not comfortable with changing my account details, but I authorize the change.",
    "I'm not comfortable with changing my account details; you have my permission.",
    "I'm not comfortable with changing my account details. Yes, go ahead.",
    "I'm not comfortable with changing my account details, but feel free to update my account.",
    "I'm not comfortable with changing my account details. Please make those changes.",
    "I'm not comfortable changing my account. However, I am authorizing you to change it.",
    "I'm not comfortable changing my account. I'm authorizing you to change it anyway.",
    "I'm not comfortable changing my account. But I would like you to update my account.",
    "I'm not comfortable changing my account. I'd like you to update my account anyway.",
])
def test_customer_discomfort_refusal_rejects_other_topics_and_affirmative_overrides(text):
    assert "USER_CONSENT_CONSTRAINT_LOST" in q.check_user_response("withhold_consent", text, [])


@pytest.mark.parametrize("text", [
    "Please let me know which matters more: fees or interest, and I will recommend a product accordingly.",
    "Tell me whether fees or interest matter more to you.",
    "Could you please indicate your preference between fees and interest.",
])
def test_clarification_can_be_an_imperative_request_without_question_punctuation(text):
    assert q.check_agent_response("clarification", text, [], []) == []


@pytest.mark.parametrize("text", [
    "Fees and interest both matter when comparing accounts.",
    "I know which matters more: fees or interest.",
    "Do not tell me whether fees or interest matter more.",
    "Please let me know which branch you use.",
])
def test_clarification_still_requires_a_relevant_request(text):
    assert "CLARIFICATION_NOT_GROUNDED_IN_REQUEST" in q.check_agent_response(
        "clarification", text, [], [])


@pytest.mark.parametrize("text", [
    "Your priority is lower monthly fees.",
    "You prefer lower monthly fees over interest.",
    "You told me that reducing monthly fees is your priority.",
    "Lower monthly fees matter most to you, not interest.",
    "Your priority is lower monthly fees. I will not ask you to repeat it.",
    "You prioritize lower monthly fees. I do not need your account number to repeat that.",
    "You don't prefer higher interest; your priority is lower monthly fees.",
])
def test_retained_preference_accepts_direct_restatement_without_unneeded_requests(text):
    assert q.check_agent_response("retained_preference", text, [], []) == []


@pytest.mark.parametrize(("text", "issue"), [
    ("I understand your priority is lower monthly fees. Please repeat the priority you gave me "
     "so I can confirm it and assist you further.", "SUPPLIED_PREFERENCE_REQUESTED_AGAIN"),
    ("Your priority is lower monthly fees; kindly restate your preference.",
     "SUPPLIED_PREFERENCE_REQUESTED_AGAIN"),
    ("Lower monthly fees matter most. Tell me whether fees or interest matter more to you.",
     "SUPPLIED_PREFERENCE_REQUESTED_AGAIN"),
    ("Your priority is lower monthly fees. I need you to confirm that preference.",
     "SUPPLIED_PREFERENCE_REQUESTED_AGAIN"),
    ("Your priority is lower monthly fees. Please confirm that again.",
     "SUPPLIED_PREFERENCE_REQUESTED_AGAIN"),
    ("Your priority is lower monthly fees. Please provide your account number to proceed.",
     "IRRELEVANT_ACCOUNT_DATA_REQUEST"),
    ("You prefer lower monthly fees. To confirm, please share your customer ID.",
     "IRRELEVANT_ACCOUNT_DATA_REQUEST"),
    ("Your priority is lower monthly fees. Can you give me your account details?",
     "IRRELEVANT_ACCOUNT_DATA_REQUEST"),
    ("Your priority is higher interest, not lower monthly fees.", "SUPPLIED_PREFERENCE_CONTRADICTED"),
    ("You prefer interest over lower monthly fees.", "SUPPLIED_PREFERENCE_CONTRADICTED"),
    ("You do not prioritize lower monthly fees.", "SUPPLIED_PREFERENCE_CONTRADICTED"),
    ("Lower monthly fees are not your priority.", "SUPPLIED_PREFERENCE_CONTRADICTED"),
    ("You prioritize lower monthly fees, but interest is your top priority.",
     "SUPPLIED_PREFERENCE_CONTRADICTED"),
    ("Your priority is higher monthly fees.", "SUPPLIED_PREFERENCE_NOT_USED"),
])
def test_retained_preference_rejects_observed_false_passes_and_paraphrases(text, issue):
    assert issue in q.check_agent_response("retained_preference", text, [], [])


@pytest.mark.parametrize("text", [
    "To be honest, I prefer not to have higher monthly fees. How about you?",
    "Lower monthly fees matter most to me. And you?",
    "I prefer lower monthly fees. What is your preference?",
    "I prefer lower monthly fees. What matters more to you?",
    "I prefer lower monthly fees. Do you prefer fees or interest?",
])
def test_customer_preference_does_not_solicit_the_representatives_personal_preference(text):
    assert "USER_ASKED_REPRESENTATIVE_PREFERENCE" in q.check_user_response("preference", text, [])


@pytest.mark.parametrize("text", [
    "I prefer not to have higher monthly fees.",
    "I prefer lower monthly fees. What do you recommend?",
    "I prefer lower monthly fees, and you can explain the available options.",
    "I do not prefer higher interest. I prefer lower monthly fees.",
])
def test_customer_preference_allows_customer_questions_and_normal_second_person_language(text):
    assert q.check_user_response("preference", text, []) == []


@pytest.mark.parametrize("text", [
    "I prefer higher interest over lower fees.",
    "I prefer high interest rather than lower monthly fees.",
    "My priority is higher interest, not lower monthly fees.",
    "I do not prefer lower monthly fees.",
    "I prefer higher monthly fees.",
])
def test_customer_preference_rejects_reversed_and_contradictory_priorities(text):
    assert "USER_PREFERENCE_CONTRADICTED" in q.check_user_response("preference", text, [])


AUTHORED_POLICY = (
    "1. Checking account closure\n   ID: authored_closure\n   Score: 0.8000\n   Content: "
    "Before closing a checking account, verify identity and settle outstanding balances.\n"
    "\n[Timing: retrieval=2ms, total=2ms]"
)
GROUNDED_ANSWER = (
    "Before closing a checking account, the policy says to verify identity and settle outstanding balances."
)


def test_grounding_requires_executed_retrieval_with_content_used_in_the_answer():
    assert q.check_agent_response(
        "grounding", GROUNDED_ANSWER, ["KB_search"], ["KB_search"],
        retrieval_results=[AUTHORED_POLICY],
    ) == []
    assert q.check_agent_response(
        "grounding", GROUNDED_ANSWER, ["grep"], ["grep"],
        retrieval_results=[json.dumps(AUTHORED_POLICY)],
    ) == []
    for calls, successful in [([], []), (["KB_search"], []), ([], ["KB_search"]),
                              (["lookup_account"], ["lookup_account"])]:
        assert "RETRIEVAL_EVIDENCE_MISSING" in q.check_agent_response(
            "grounding", GROUNDED_ANSWER, calls, successful, retrieval_results=[AUTHORED_POLICY],
        )


@pytest.mark.parametrize("text", [
    "The retrieved policy explains the requirements.",
    "The retrieved policy covers checking-account closure. Which checking account would you like help with?",
    "I will check identity and outstanding balances before closing your checking account.",
    "Please wait while I verify identity and settle outstanding balances for closing a checking account.",
    "Which checking account would you like to close after verifying identity and settling outstanding balances?",
    "To close a checking account, visit a branch with your passport and pay a cash fee.",
    "The checking account closure result has ID authored_closure and score 0.8000.",
])
def test_search_alone_generic_promises_and_unsupported_answers_do_not_pass_grounding(text):
    assert "RETRIEVAL_ANSWER_NOT_GROUNDED" in q.check_agent_response(
        "grounding", text, ["KB_search"], ["KB_search"], retrieval_results=[AUTHORED_POLICY],
    )


@pytest.mark.parametrize("result", ["", "   ", "[]", "{}", "null", "No relevant documents found.",
                                   "No matches found for pattern: closing a checking account"])
def test_empty_retrieval_is_not_policy_evidence_even_with_successful_execution(result):
    issues = q.check_agent_response("grounding", GROUNDED_ANSWER, ["KB_search"], ["KB_search"],
                                    retrieval_results=[result])
    assert "RETRIEVAL_CONTENT_MISSING" in issues
    assert "RETRIEVAL_ANSWER_NOT_GROUNDED" in issues


def test_missing_and_uncertain_requests_never_become_a_pass_or_zero_score():
    schedule = q.probe_schedule()
    partial = q.summarize([record(schedule[0])], scheduled=24, deadline_seconds=90)
    assert partial["status"] == "failed" and partial["not_run_probes"] == 23
    assert partial["recorded_checks_status"] == "passed" and partial["coverage_complete"] is False
    initial = q.summarize([], scheduled=24, deadline_seconds=90)
    assert initial["recorded_checks_status"] == "not_started"
    rows = [record(probe) for probe in schedule]
    rows[-1] = record(schedule[-1], passed=False, seconds=90.1, status="UNCERTAIN")
    summary = q.summarize(rows, scheduled=24, deadline_seconds=90)
    assert summary["status"] == "failed" and summary["uncertain_requests"] == 1
    assert summary["capability_score"] is None
    assert summary["p95_completed_seconds"] == 10.0


def test_margin_is_a_gate_even_when_all_completed_responses_pass():
    rows = [record(probe, seconds=80) for probe in q.probe_schedule()]
    result = q.summarize(rows, scheduled=24, deadline_seconds=90)
    assert result["passed_probes"] == 24
    assert result["status"] == "failed" and not result["latency_margin_passed"]


@pytest.mark.parametrize("change", ["no_requests", "missing_usage", "negative_time", "wrong_role", "extra_requests"])
def test_passed_labels_cannot_replace_complete_bounded_request_evidence(change):
    rows = [record(probe) for probe in q.probe_schedule()]
    request = rows[0]["requests"][0]
    if change == "no_requests":
        rows[0]["requests"] = []
    elif change == "missing_usage":
        request["input_tokens"] = None
    elif change == "negative_time":
        request["seconds"] = -1
    elif change == "wrong_role":
        request["role"] = "user"
    else:
        rows[0]["requests"] *= 5
    result = q.summarize(rows, scheduled=24, deadline_seconds=90)
    assert result["status"] == "failed" and result["request_evidence_valid"] is False


def evidence(tmp_path, monkeypatch):
    from safe_agent.benchmarks.tau3 import runner

    monkeypatch.setattr(runner, "_repository_revision", lambda: {
        "runtime_sources_sha256": "sha256:" + "a" * 64,
    })
    condition = q.qualification_condition(config(tmp_path))
    budgets = q._qualification_budgets(config(tmp_path), 3600)
    rows = [record(probe) for probe in q.probe_schedule()]
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    for row in rows:
        row["trace_sha256"] = q._new_json(private / (row["probe_id"] + ".json"), {
            "authored_unit_test_fixture": True, "probe_id": row["probe_id"],
        })
    report = {
        "schema_version": 1, "kind": "authored_local_qualification", "profile": q.PROFILE,
        "condition": condition, "condition_sha256": wire_digest(condition),
        "max_seconds": 3600,
        "effective_qualification_budgets": budgets,
        "effective_qualification_budgets_sha256": wire_digest(budgets),
        "probes": rows, "finished_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        **q.summarize(rows, scheduled=24, deadline_seconds=90),
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    review = {
        "schema_version": 1, "approved": True,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "reviewed_probe_ids": [probe.probe_id for probe in q.probe_schedule()],
        "reviewer": "authored-test-reviewer", "reviewed_at": datetime.now(UTC).isoformat(),
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review))
    return report_path, review_path, report, review


def test_launch_gate_requires_separate_review_and_matching_immutable_condition(tmp_path, monkeypatch):
    report_path, review_path, _, _ = evidence(tmp_path, monkeypatch)
    with pytest.raises(q.QualificationError, match="SEMANTIC_REVIEW_REQUIRED"):
        q.require_qualification(config(tmp_path), report_path, None)
    accepted = q.require_qualification(config(tmp_path), report_path, review_path)
    assert accepted["review_is_independent_attestation"] is False
    monkeypatch.setattr(q, "qualification_condition", lambda _: {"model": "different"})
    with pytest.raises(q.QualificationError, match="CONDITION_MISMATCH"):
        q.require_qualification(config(tmp_path), report_path, review_path)


@pytest.mark.parametrize("changes", [
    {"system": "llm_agent"},
    {"runtime_factory": "example.runtime:factory"},
    {"retrieval_config_kwargs": {"top_k": 2}},
    {"episode_budget": {"max_tokens": 1}},
    {"episode_budget": {"max_tool_calls": 5}},
    {"run_budget": {"max_model_calls": 1}},
    {"run_budget": {"deadline_seconds": 600}},
])
def test_launch_gate_rejects_unexercised_scope_retrieval_and_runtime_budgets(tmp_path, monkeypatch, changes):
    report_path, review_path, _, _ = evidence(tmp_path, monkeypatch)
    with pytest.raises(q.QualificationError, match="CONDITION_MISMATCH"):
        q.require_qualification(config(tmp_path, **changes), report_path, review_path)


def test_run_identity_output_and_task_progression_do_not_change_model_condition(tmp_path, monkeypatch):
    report_path, review_path, _, _ = evidence(tmp_path, monkeypatch)
    later = config(tmp_path, run_id="five-task-pilot", output_dir=str(tmp_path / "five-tasks"),
                   task_ids=[f"task_{i:03}" for i in range(1, 6)],
                   development_task_ids=[f"task_{i:03}" for i in range(1, 6)])
    assert q.require_qualification(later, report_path, review_path)["condition_sha256"]


@pytest.mark.parametrize("change", ["changed", "deleted", "symlink"])
def test_review_cannot_authorize_changed_or_missing_private_trace(tmp_path, monkeypatch, change):
    report_path, review_path, report, _ = evidence(tmp_path, monkeypatch)
    trace = tmp_path / "private" / (report["probes"][1]["probe_id"] + ".json")
    if change == "changed":
        trace.write_text('{"tool_calls":[{"name":"different","arguments":{}}]}')
    elif change == "deleted":
        trace.unlink()
    else:
        copy = tmp_path / "same-trace.json"
        trace.rename(copy)
        trace.symlink_to(copy)
    with pytest.raises(q.QualificationError, match="TRACE_INVALID"):
        q.require_qualification(config(tmp_path), report_path, review_path)


@pytest.mark.parametrize("change", ["changed_budget", "missing_budget", "changed_digest", "bad_wall_budget"])
def test_even_reviewed_report_must_bind_actual_qualification_budgets(tmp_path, monkeypatch, change):
    report_path, review_path, report, review = evidence(tmp_path, monkeypatch)
    if change == "changed_budget":
        report["effective_qualification_budgets"]["episode_budget"]["max_model_calls"] = 5
        report["effective_qualification_budgets_sha256"] = wire_digest(report["effective_qualification_budgets"])
    elif change == "missing_budget":
        del report["effective_qualification_budgets"]
    elif change == "changed_digest":
        report["effective_qualification_budgets_sha256"] = "sha256:" + "b" * 64
    else:
        report["max_seconds"] = True
    report_path.write_text(json.dumps(report))
    review["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    review_path.write_text(json.dumps(review))
    with pytest.raises(q.QualificationError):
        q.require_qualification(config(tmp_path), report_path, review_path)


def test_effective_budgets_only_reduce_requested_allowances_and_record_all_limits(tmp_path):
    configured = config(tmp_path, episode_budget={"max_model_calls": 2, "max_tokens": 12_345},
                        run_budget={"max_model_calls": 13, "deadline_seconds": 500})
    requested = q._requested_runtime_budgets(configured)
    effective = q._qualification_budgets(configured, 600)
    assert effective["episode_budget"] == requested["episode_budget"]
    assert effective["run_budget"] == requested["run_budget"]
    assert effective["episode_budget"]["max_tokens"] == 12_345
    assert "max_attempt_cost_microusd" in effective["episode_budget"]
    assert "max_tool_calls" in effective["run_budget"]
    default = q._qualification_budgets(config(tmp_path), 600.8)
    assert default["episode_budget"]["max_model_calls"] == 4
    assert default["run_budget"]["max_model_calls"] == 36
    assert default["run_budget"]["deadline_seconds"] == 600


def test_pending_admitted_request_is_preserved_after_earlier_completed_reply():
    now = datetime.now(UTC)

    def event(kind, request_id, offset):
        return SimpleNamespace(kind=SimpleNamespace(value=kind), payload={"request_id": request_id},
                               occurred_at=now - timedelta(seconds=offset))

    events = [event("INFERENCE_RESERVED", "r1", 12), event("INFERENCE_FINISHED", "r1", 11),
              event("INFERENCE_RESERVED", "r2", 10)]
    request = SimpleNamespace(projected_context={"fixture": True})
    reply = SimpleNamespace(input_tokens=100, output_tokens=20)
    runtime = SimpleNamespace(
        episodes=SimpleNamespace(events=lambda _: events),
        private=SimpleNamespace(inference=lambda rid: (request, reply, "COMPLETE") if rid == "r1"
                                else (request, None, "PENDING")),
    )
    rows = q._request_records(runtime, "authored_fixture")
    assert [row["request_id"] for row in rows] == ["r1", "r2"]
    assert [row["status"] for row in rows] == ["COMPLETE", "INCOMPLETE"]


@pytest.mark.parametrize("change", ["changed_report", "incomplete", "forged_summary", "stale_review"])
def test_launch_gate_rejects_incomplete_or_changed_evidence(tmp_path, monkeypatch, change):
    report_path, review_path, report, review = evidence(tmp_path, monkeypatch)
    if change == "changed_report":
        report["new_field"] = "changed after review"
    elif change == "incomplete":
        report["probes"].pop()
    elif change == "forged_summary":
        report["probes"][0]["requests"][0]["seconds"] = 95
        report["probes"][1]["requests"][0]["seconds"] = 95
    else:
        review["reviewed_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    report_path.write_text(json.dumps(report))
    review_path.write_text(json.dumps(review))
    with pytest.raises(q.QualificationError):
        q.require_qualification(config(tmp_path), report_path, review_path)


@pytest.mark.parametrize("change", ["empty", "missing", "missing_usage"])
def test_reviewed_pass_label_cannot_replace_missing_request_evidence(tmp_path, monkeypatch, change):
    report_path, review_path, report, review = evidence(tmp_path, monkeypatch)
    probe = report["probes"][0]
    if change == "empty":
        probe["requests"] = []
    elif change == "missing":
        del probe["requests"]
    else:
        del probe["requests"][0]["output_tokens"]
    report_path.write_text(json.dumps(report))
    review["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    review_path.write_text(json.dumps(review))
    with pytest.raises(q.QualificationError):
        q.require_qualification(config(tmp_path), report_path, review_path)


def test_failed_probe_stops_before_next_model_call_and_keeps_missing_schedule(tmp_path, monkeypatch):
    from safe_agent.benchmarks.tau3 import local_harness, preflight, runner
    from safe_agent.orchestration import episode_runtime

    monkeypatch.setattr(preflight, "preflight_run", lambda *a, **k: {"ready_for_attempt": True})
    monkeypatch.setattr(runner, "prepare_official_import", lambda _: None)
    monkeypatch.setattr(q, "qualification_condition", lambda _: {"authored": True})
    monkeypatch.setattr(local_harness, "harness_transport", lambda _: nullcontext())
    disposed = []
    contexts = []

    def make_runtime(context):
        contexts.append(context)
        return SimpleNamespace(episodes=SimpleNamespace(
            engine=SimpleNamespace(dispose=lambda: disposed.append(True))))

    monkeypatch.setattr(episode_runtime, "runtime_factory", make_runtime)
    probes = []

    def fail(probe, *_):
        probes.append(probe.probe_id)
        return record(probe, passed=False)

    monkeypatch.setattr(q, "_run_agent_probe", fail)
    monkeypatch.setattr(q, "_run_user_probe", lambda *a: pytest.fail("Second inference was dispatched"))
    output = tmp_path / "qualification"
    report = q.run_qualification(config(tmp_path), output, tau_root=tmp_path / "source", max_seconds=600)
    assert len(probes) == 1 and disposed == [True]
    assert report["status"] == "failed" and report["not_run_probes"] == 23
    assert report["recorded_checks_status"] == "failed" and report["coverage_complete"] is False
    assert report["stop_reason"] == "QUALIFICATION_PROBE_FAILED"
    assert json.loads((output / "report.json").read_text()) == report
    assert output.stat().st_mode & 0o077 == 0
    assert contexts[0].run_budget["deadline_seconds"] == 600
    assert contexts[0].run_budget["max_model_calls"] == 36
    effective = report["effective_qualification_budgets"]
    assert contexts[0].run_budget == effective["run_budget"]
    assert contexts[0].episode_budget == effective["episode_budget"]
    assert report["effective_qualification_budgets_sha256"] == wire_digest(effective)


@pytest.mark.parametrize("interrupt_after_last", [False, True])
def test_progress_reports_stay_running_until_finalization(tmp_path, monkeypatch, interrupt_after_last):
    from safe_agent.benchmarks.tau3 import local_harness, preflight, runner
    from safe_agent.orchestration import episode_runtime

    monkeypatch.setattr(preflight, "preflight_run", lambda *a, **k: {"ready_for_attempt": True})
    monkeypatch.setattr(runner, "prepare_official_import", lambda _: None)
    monkeypatch.setattr(q, "qualification_condition", lambda _: {"authored": True})
    monkeypatch.setattr(local_harness, "harness_transport", lambda _: nullcontext())
    monkeypatch.setattr(episode_runtime, "runtime_factory", lambda _: SimpleNamespace(
        episodes=SimpleNamespace(engine=SimpleNamespace(dispose=lambda: None))))
    monkeypatch.setattr(q, "_run_agent_probe", lambda probe, *a: record(probe))
    monkeypatch.setattr(q, "_run_user_probe", lambda probe, *a: record(probe))
    output = tmp_path / "qualification"
    snapshots = []

    def progress(event):
        snapshot = json.loads((output / "report.json").read_text())
        assert snapshot["status"] == "running"
        assert "finished_at" not in snapshot
        snapshots.append(snapshot)
        if interrupt_after_last and snapshot["recorded_probes"] == 24:
            raise KeyboardInterrupt

    report = q.run_qualification(config(tmp_path), output, tau_root=tmp_path / "source",
                                 max_seconds=600, progress=progress)
    assert snapshots[0]["recorded_checks_status"] == "not_started"
    assert snapshots[1]["recorded_checks_status"] == "passed"
    assert snapshots[1]["not_run_probes"] == 23
    assert report["recorded_checks_status"] == "passed" and report["coverage_complete"] is True
    assert "finished_at" in report and json.loads((output / "report.json").read_text()) == report
    if interrupt_after_last:
        assert report["status"] == "failed"
        assert report["stop_reason"] == "QUALIFICATION_INTERRUPTED"
    else:
        assert report["status"] == "automated_checks_passed"
        assert "stop_reason" not in report


@pytest.mark.parametrize("authored_input", [None, "Please ask whether fees or interest matter more."])
def test_agent_probe_applies_exact_retrieval_condition_and_binds_trace(tmp_path, monkeypatch, authored_input):
    pytest.importorskip("tau2", reason="Pinned benchmark dependency is optional in office CI")
    from tau2.data_model.message import AssistantMessage
    from tau2.domains.banking_knowledge import environment
    from tau2.orchestrator import orchestrator

    from safe_agent.benchmarks.tau3 import adapter

    observed = []

    def get_environment(**kwargs):
        observed.append(kwargs)
        return SimpleNamespace(get_tools=lambda: [], get_policy=lambda: "Authored fixture policy")

    class FixtureOrchestrator:
        def __init__(self, **kwargs):
            self.history = kwargs["task"].initial_state.message_history
            if authored_input is not None:
                assert self.history[-1].content == authored_input

        def run(self):
            return SimpleNamespace(messages=[*self.history, AssistantMessage(
                role="assistant", content="Do fees or interest matter more to you?",
            )])

    probe = q.probe_schedule()[0]
    monkeypatch.setattr(environment, "get_environment", get_environment)
    monkeypatch.setattr(orchestrator, "Orchestrator", FixtureOrchestrator)
    monkeypatch.setattr(adapter, "official_agent_class", lambda: lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(q, "_request_records", lambda *args: record(probe)["requests"])
    result = q._run_agent_probe(probe, config(tmp_path, retrieval_config_kwargs={"top_k": 2}),
                                SimpleNamespace(), tmp_path, authored_input=authored_input)
    assert result["passed"] is True
    assert observed == [{"retrieval_variant": "bm25", "retrieval_kwargs": {"top_k": 2}}]
    raw = (tmp_path / (probe.probe_id + ".json")).read_bytes()
    assert result["trace_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["diagnostic_only"] is (authored_input is not None)
    if authored_input is not None:
        assert json.loads(raw)["diagnostic_only"] is True
        assert json.loads(raw)["authored_input"] == authored_input


def test_user_probe_applies_declared_seed_and_exact_local_route_without_model_calls(tmp_path, monkeypatch):
    pytest.importorskip("tau2", reason="Pinned benchmark dependency is optional in office CI")
    from tau2.user import user_simulator

    observed = {}

    class FixtureUser:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        def set_seed(self, seed):
            observed["applied_seed"] = seed

        def get_init_state(self, **kwargs):
            return kwargs

        def generate_next_message(self, message, state):
            return SimpleNamespace(content="I prefer lower monthly fees.", tool_calls=None,
                                   usage={"input_tokens": 4500, "output_tokens": 20}), state

    monkeypatch.setattr(user_simulator, "UserSimulator", FixtureUser)
    probe = q.probe_schedule()[1]
    result = q._run_user_probe(probe, config(tmp_path, seed=417), tmp_path)
    assert result["passed"] is True
    assert observed["applied_seed"] == 417
    assert observed["llm"] == "openai/local-test"
    assert observed["llm_args"]["api_base"] == "http://127.0.0.1:11434/v1"
    assert observed["llm_args"]["api_key"] == "safe-local-no-key"
    assert observed["llm_args"]["num_retries"] == 0
    assert observed["llm_args"]["timeout"] == 90
    raw = (tmp_path / (probe.probe_id + ".json")).read_bytes()
    assert result["trace_sha256"] == hashlib.sha256(raw).hexdigest()


def test_existing_output_and_hosted_route_are_rejected_before_preflight(tmp_path, monkeypatch):
    from safe_agent.benchmarks.tau3 import preflight

    monkeypatch.setattr(preflight, "preflight_run", lambda *a, **k: pytest.fail("Preflight contacted"))
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(q.QualificationError, match="OUTPUT_MUST_BE_NEW"):
        q.run_qualification(config(tmp_path), existing)
    with pytest.raises(q.QualificationError, match="EXPLICIT_LOCAL"):
        q.run_qualification(BenchmarkConfig(), tmp_path / "new")


def test_cli_failed_qualification_blocks_before_benchmark_run(tmp_path, monkeypatch, capsys):
    from safe_agent.benchmarks.tau3 import __main__ as cli
    from safe_agent.benchmarks.tau3 import runner

    monkeypatch.setattr(cli, "load_config", lambda _: config(tmp_path))
    monkeypatch.setattr(runner, "inspect_benchmark", lambda *a, **k: pytest.fail("Benchmark dispatched"))
    monkeypatch.setattr(q, "require_qualification", lambda *a: (_ for _ in ()).throw(
        q.QualificationError("QUALIFICATION_NOT_PASSED")))
    code = cli.main(["run", "--config", "config.toml", "--qualification-report", "report.json",
                     "--qualification-review", "review.json"])
    assert code == 2 and "QUALIFICATION_NOT_PASSED" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["v2", "v3", "v4", "diagnostic", "stopped"])
def test_prior_profile_diagnostic_and_stopped_reports_cannot_qualify(tmp_path, monkeypatch, change):
    report_path, review_path, report, review = evidence(tmp_path, monkeypatch)
    if change in {"v2", "v3", "v4"}:
        report["profile"] = f"authored_local_role_qualification_{change}"
    elif change == "diagnostic":
        report["probes"][0]["diagnostic_only"] = True
    else:
        report["stop_reason"] = "QUALIFICATION_INTERRUPTED"
    report_path.write_text(json.dumps(report))
    review["report_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    review_path.write_text(json.dumps(review))
    with pytest.raises(q.QualificationError):
        q.require_qualification(config(tmp_path), report_path, review_path)
