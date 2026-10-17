"""Mandatory disposable PostgreSQL coverage for W4; no SQLite substitution."""
import os

import pytest

from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.tal.office_gate import OfficeGate
from tests.m7 import test_coordinated_planning as cases
from tests.m7 import test_coordinated_planning_process as recovery_cases
from tests.m7 import test_inference_admission_postgres as admission_cases
from tests.m7.test_coordinated_planning_schema import assert_planning_schema

pg_repo = admission_cases.pg_repo
pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_PLANNING") != "1",
    reason="requires explicit disposable PostgreSQL planning job",
)


@pytest.mark.parametrize("case", [
    cases.test_operator_prepares_exact_final_action_then_requires_separate_approval,
    cases.test_restart_after_validated_result_reuses_input_and_never_sends_again,
    cases.test_restart_after_input_before_admission_keeps_same_attempt,
    cases.test_uncertain_call_cannot_be_replaced_after_lease_expiry_or_generic_failure,
    cases.test_consumption_insert_failure_rolls_back_preview_proposal_and_outcome,
    cases.test_lost_consumption_commit_ack_recovers_committed_preparation,
    cases.test_expired_validated_result_cannot_prepare_or_mint_replacement,
    cases.test_revoked_reference_at_consumption_prevents_proposal,
    cases.test_same_revision_changed_source_content_is_rejected,
    cases.test_parallel_operator_requests_have_one_inference_and_consumption,
    cases.test_input_and_consumption_are_database_immutable,
    cases.test_clarification_is_fixed_and_repeat_does_not_start_another_attempt,
    recovery_cases.test_saved_input_revoked_before_admission_never_calls_inference,
    recovery_cases.test_slow_consumption_transaction_rolls_back_when_deadline_passes,
])
def test_postgres_coordinated_planning(pg_repo, kit, monkeypatch, case):
    # The real forward migrations create a fresh schema for each case.
    assert_planning_schema(pg_repo.engine)
    scoped = {**kit, "repo": pg_repo,
              "coordinator": TrustedOfficeTasks(pg_repo, kit["office"], OfficeGate(kit["broker"]))}
    case(scoped, monkeypatch)
