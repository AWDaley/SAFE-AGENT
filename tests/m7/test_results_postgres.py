"""Mandatory W3 PostgreSQL result acceptance; uses fresh migrated schemas."""
import os

import pytest

from tests.m7 import test_inference_admission_postgres as admission_cases
from tests.m7 import test_inference_results as cases
from tests.m7 import test_result_process_and_races as race_cases
from tests.m7.test_result_schema import assert_schema

pg_repo = admission_cases.pg_repo
pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_RESULTS") != "1",
    reason="requires disposable PostgreSQL result acceptance job",
)


@pytest.mark.parametrize("case", [
    cases.test_exact_result_snapshot_recovery_has_no_effect_or_budget_release,
    cases.test_update_delete_and_duplicate_insert_are_blocked_by_database,
    cases.test_parallel_recovery_returns_same_record_without_duplicate_rows,
    cases.test_clarification_persists_only_fixed_safe_text,
    cases.test_changed_prompt_policy_or_regressed_clock_cannot_recover,
    cases.test_slow_recovery_commit_cannot_return_expired_candidate,
    race_cases.test_competing_finishers_record_only_one_result,
    race_cases.test_sql_result_metadata_must_match_validation_event,
])
def test_postgres_result_invariants(pg_repo, case):
    assert_schema(pg_repo.engine)
    case(pg_repo)


def test_postgres_result_insert_rollback(pg_repo, monkeypatch):
    cases.test_result_insert_failure_rolls_back_validation_and_prevents_resend(pg_repo, monkeypatch)


@pytest.mark.parametrize("change", ["tenant", "actor", "scope", "expired_actor", "attempt",
                                    "workspace", "manifest", "attempt_number"])
def test_postgres_recovery_scope(pg_repo, change):
    cases.test_recovery_rejects_foreign_or_forged_identity(pg_repo, change)
