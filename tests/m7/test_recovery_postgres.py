"""Proposed mandatory isolated PostgreSQL regression job; never live accounts."""
import os

import pytest

from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.tal.office_gate import OfficeGate
from tests.m7 import test_inference_admission_postgres as admission_cases
from tests.m7 import test_planning_recovery as cases
from tests.m7 import test_task_retirement as retirement_cases

pg_repo = admission_cases.pg_repo
pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_RECOVERY") != "1",
    reason="requires explicit disposable PostgreSQL recovery job",
)


@pytest.mark.parametrize("case", [
    cases.test_status_does_not_create_attempt_or_admission,
    cases.test_recoverable_reads_have_no_sql_mutations_or_private_payload,
    cases.test_resume_uses_saved_result_without_inference_approval_or_outbox,
    cases.test_unknown_remains_uncertain_after_deadline_and_never_refunds,
    cases.test_started_status_is_observational_and_does_not_rewrite_history,
    cases.test_expired_result_is_not_resumable,
    cases.test_revocation_between_inspection_and_resume_is_rechecked,
    cases.test_same_tenant_reader_cannot_resume_and_foreign_task_is_hidden,
    cases.test_closed_resume_command_and_state_revision_are_enforced,
    cases.test_lost_commit_ack_reads_existing_consumption,
    cases.test_simultaneous_resume_commands_consume_once,
    cases.test_recovery_responses_have_no_store_headers,
    cases.test_input_saved_without_admission_cannot_be_dispatched_by_resume,
    retirement_cases.test_not_started_task_retires_terminally_and_new_task_is_separate,
    retirement_cases.test_reserved_invocation_is_cancelled_before_dispatch_but_hold_is_preserved,
    retirement_cases.test_started_inference_cannot_retire_before_deadline,
    retirement_cases.test_expired_started_inference_becomes_unknown_then_task_retires_without_refund,
    retirement_cases.test_saved_result_can_be_retired_instead_of_consumed,
    retirement_cases.test_prepared_task_can_retire_before_approval_or_queue,
])
def test_postgres_recovery(pg_repo, kit, monkeypatch, case):
    scoped = {**kit, "repo": pg_repo,
              "coordinator": TrustedOfficeTasks(pg_repo, kit["office"], OfficeGate(kit["broker"]))}
    case(scoped, monkeypatch)
