"""Guard M7-04 integration against the conflicting PR #29 SQL revision.

These tests exercise the real Alembic graph and PR #27's physical schema;
a passing unit mock alone would not detect an accidental migration fork.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "check_m7_budget_lineage.py"


def _run(args: list[str], *, environment: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        args, cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=35,
        check=False,
    )
    assert result.returncode == 0, (
        "canonical M7 migration/sidecar integration check failed: "
        + result.stdout[-1000:] + result.stderr[-1000:]
    )


def test_pr28_inherits_only_pr27_alembic_migration_and_billing_interface() -> None:
    _run([sys.executable, str(CHECK)])


def test_real_0009_upgrade_has_exact_pr27_schema_and_single_head(tmp_path: Path) -> None:
    database = tmp_path / "canonical_budget.sqlite"
    environment = {
        **os.environ,
        "SAFE_AGENT_DATABASE_URL": f"sqlite+pysqlite:///{database}",
    }
    _run([sys.executable, "-m", "alembic", "upgrade", "head"], environment=environment)
    _run([sys.executable, str(CHECK), "--verify-database"], environment=environment)
