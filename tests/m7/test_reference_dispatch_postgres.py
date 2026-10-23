"""Reference/dispatch fences on real PostgreSQL and existing forward migrations."""
import os

import pytest

from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.tal.office_gate import OfficeGate
from tests.m7 import test_inference_admission_postgres as admission_cases
from tests.m7 import test_reference_dispatch as cases

pg_repo = admission_cases.pg_repo
pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_REVIEW") != "1",
    reason="requires explicit disposable PostgreSQL review-boundary job",
)


@pytest.mark.parametrize("case,options", [
    pytest.param(cases.test_revocation_before_final_admission_prevents_new_effect,
                 {"when": "before_worker"}, id="revoked_before_worker"),
    pytest.param(cases.test_revocation_before_final_admission_prevents_new_effect,
                 {"when": "after_readback"}, id="revoked_after_readback"),
    pytest.param(cases.test_revocation_before_final_admission_prevents_new_effect,
                 {"when": "after_source_check"}, id="revoked_after_source_check"),
    *[pytest.param(cases.test_current_reference_rows_are_required_even_after_initial_readback,
                   {"mutation": mutation}, id="reference_" + mutation)
      for mutation in ("row_flag", "envelope_flag", "bad_mac", "missing")],
    pytest.param(cases.test_committed_dispatch_admission_precedes_later_revocation,
                 {}, id="admitted_before_revoke"),
    *[pytest.param(cases.test_crash_after_admission_requires_fresh_authority_when_effect_absent,
                   {"revoked": revoked}, id="crash_before_effect_revoked_" + str(revoked))
      for revoked in (False, True)],
    *[pytest.param(cases.test_existing_effect_is_verified_after_revocation_and_restart,
                   {"internal_task": internal}, id="committed_readback_internal_" + str(internal))
      for internal in (False, True)],
    pytest.param(cases.test_internal_task_checks_current_references_before_new_write,
                 {}, id="internal_task_revoked"),
    pytest.param(cases.test_expired_claim_cannot_use_reacquired_lease_with_same_worker_id,
                 {}, id="same_worker_stale_claim"),
    pytest.param(cases.test_superseded_process_cannot_finalize_an_already_admitted_effect,
                 {}, id="same_worker_superseded_dispatch"),
    *[pytest.param(cases.test_result_transaction_keeps_claim_fenced_until_commit,
                   {"outcome": outcome}, id="result_lock_" + outcome)
      for outcome in ("verified", "review", "unknown")],
    *[pytest.param(cases.test_dispatch_and_revocation_serialize_on_task_lock,
                   {"first": first}, id="sql_lock_" + first + "_first")
      for first in ("dispatch", "revocation")],
    pytest.param(cases.test_readback_resolution_preserves_integrity_scope_and_revocation,
                 {}, id="readback_scope_integrity"),
    pytest.param(cases.test_supported_revocation_cannot_rebind_resource_identity,
                 {}, id="immutable_revoked_binding"),
    pytest.param(cases.test_dispatching_is_pending_in_tal_audit_and_operator_status,
                 {}, id="pending_audit_and_operator_status"),
])
def test_postgres_reference_dispatch(pg_repo, kit, monkeypatch, case, options):
    scoped = {**kit, "repo": pg_repo,
              "coordinator": TrustedOfficeTasks(pg_repo, kit["office"], OfficeGate(kit["broker"]))}
    case(scoped, monkeypatch, **options)
