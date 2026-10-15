"""Mandatory migrated PostgreSQL dispatch acceptance, separate from SQLite."""
import os

import pytest

from tests.m7 import test_inference_coordinator as cases
from tests.m7.test_inference_admission_postgres import pg_repo  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_COORDINATOR") != "1",
    reason="requires explicit disposable PostgreSQL coordinator acceptance job",
)


@pytest.mark.parametrize("case", [
    cases.test_start_and_finish_keep_canonical_cost_holds,
    cases.test_stale_claim_is_fenced_after_safe_predispatch_reclaim,
    cases.test_unknown_never_resends_or_releases_capacity,
    cases.test_expired_reserved_cannot_be_started_by_paused_worker,
    cases.test_start_rollback_prevents_marker_and_preserves_claim,
    cases.test_concurrent_claims_have_one_owner,
])
def test_migrated_postgres_dispatch(pg_repo, case):
    case(pg_repo)
