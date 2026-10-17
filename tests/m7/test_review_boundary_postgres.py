"""Mandatory real PostgreSQL R1/R2 coverage; each case uses the forward migrations."""
import os

import pytest

from safe_agent.orchestration.office_tasks import TrustedOfficeTasks
from safe_agent.tal.office_gate import OfficeGate
from tests.m7 import test_coordinated_queue_mode as routing
from tests.m7 import test_inference_admission_postgres as admission_cases
from tests.m7 import test_review_boundary_acceptance as acceptance
from tests.m7 import test_review_boundary_regressions as regressions
from tests.m7.test_coordinated_planning_schema import assert_planning_schema

pg_repo = admission_cases.pg_repo
pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_REVIEW") != "1",
    reason="requires explicit disposable PostgreSQL review-boundary job",
)


@pytest.mark.parametrize("case,options", [
    pytest.param(regressions.test_rejected_restore_never_publishes_changed_fingerprint,
                 {"fresh_process": False}, id="restore_existing_cache"),
    pytest.param(regressions.test_rejected_restore_never_publishes_changed_fingerprint,
                 {"fresh_process": True}, id="restore_empty_cache"),
    pytest.param(regressions.test_queue_checks_durable_baseline_even_if_cache_is_corrupted,
                 {}, id="durable_queue_baseline"),
    pytest.param(regressions.test_issued_revision_fingerprint_cannot_be_rebound,
                 {}, id="issued_binding_immutable"),
    pytest.param(regressions.test_coordinated_worker_without_tal_cannot_create_an_effect,
                 {}, id="missing_checker"),
    pytest.param(acceptance.test_restore_failure_is_cache_neutral_for_either_source,
                 {"changed_side": "office"}, id="local_source_changed"),
    pytest.param(acceptance.test_restore_failure_is_cache_neutral_for_either_source,
                 {"changed_side": "provider"}, id="provider_source_changed"),
    pytest.param(acceptance.test_real_pinned_tal_checks_new_coordinated_effect,
                 {"restart": False}, id="real_pinned_tal"),
    pytest.param(acceptance.test_real_pinned_tal_checks_new_coordinated_effect,
                 {"restart": True}, id="real_pinned_tal_after_restart"),
    pytest.param(acceptance.test_incompatible_checker_cannot_authorize_coordinated_effect,
                 {"bad_checker": "object"}, id="incompatible_checker"),
    pytest.param(acceptance.test_incompatible_checker_cannot_authorize_coordinated_effect,
                 {"bad_checker": "subclass"}, id="subclass_checker"),
    pytest.param(acceptance.test_missing_denying_or_incompatible_tal_blocks_new_effect,
                 {"failure": "import"}, id="missing_runtime"),
    pytest.param(acceptance.test_missing_denying_or_incompatible_tal_blocks_new_effect,
                 {"failure": "denying"}, id="denying_runtime"),
    pytest.param(acceptance.test_missing_denying_or_incompatible_tal_blocks_new_effect,
                 {"failure": "incompatible"}, id="incompatible_runtime"),
    pytest.param(acceptance.test_missing_denying_or_incompatible_tal_blocks_new_effect,
                 {"failure": "unreviewed_pin"}, id="unreviewed_runtime"),
    pytest.param(acceptance.test_committed_effect_readback_needs_no_new_tal_authority,
                 {}, id="existing_effect_readback"),
    pytest.param(acceptance.test_legacy_inline_gateway_cannot_bypass_coordinated_worker,
                 {}, id="inline_dispatch_denied"),
    pytest.param(routing.test_coordinated_api_disables_legacy_prepare,
                 {}, id="legacy_prepare_api_denied"),
    pytest.param(routing.test_coordinated_api_does_not_adopt_legacy_prepared_task,
                 {}, id="legacy_queue_adoption_denied"),
])
def test_postgres_review_boundaries(pg_repo, kit, monkeypatch, case, options):
    assert_planning_schema(pg_repo.engine)
    scoped = {**kit, "repo": pg_repo,
              "coordinator": TrustedOfficeTasks(pg_repo, kit["office"], OfficeGate(kit["broker"]))}
    case(scoped, monkeypatch, **options)
