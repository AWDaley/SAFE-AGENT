"""OAuth one-use state checks against explicitly selected disposable PostgreSQL."""
import os

import pytest

from tests.m5 import test_oauth_atomic as cases
from tests.m7 import test_inference_admission_postgres as admission_cases

pg_repo = admission_cases.pg_repo
pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_POSTGRES_OAUTH") != "1",
    reason="requires explicit disposable PostgreSQL OAuth acceptance job",
)


def test_postgres_oauth_state_has_exactly_one_consumer(pg_repo):
    cases.assert_single_consumer(pg_repo.engine)


@pytest.mark.parametrize("case", cases.DENIAL_CASES)
def test_postgres_oauth_state_preserves_scope_and_expiry(pg_repo, case):
    cases.assert_scoped_state_denial(pg_repo.engine, case)


def test_postgres_oauth_commit_failure_releases_no_state(pg_repo):
    cases.assert_commit_failure_releases_no_state(pg_repo.engine)
