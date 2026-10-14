"""Conservative M5 release-gate inspection: offline configuration is not live acceptance.

This inspector validates checked-in CI/test wiring, not Google service state.
No argument, environment flag or locally authored result can certify a live
Google effect. Release acceptance requires a separate, independently reviewed
implementation and evidence-validation change.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field

from safe_agent.contracts.base import StrictModel

TAL_REPOSITORY = "https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git"
PIN_FILE = Path("ci/tal-office-commit.sha")
WORKFLOWS = (
    Path(".github/workflows/ci.yml"),
    Path(".github/workflows/tal-office-journal.yml"),
)
REQUIRED_CROSS_REPO_TESTS = (
    "tests/m4/test_tal_office_journal.py",
    "tests/m4/test_tal_runtime_dispatch.py",
    "tests/m5/test_tal_execution_authority.py",
    "tests/m5/test_google_test_outbox.py",
    "tests/m5/test_google_recovery.py",
    "tests/m5/test_google_provider_binding.py",
    "tests/m5/test_google_acceptance_preflight.py",
)
REQUIRED_SOURCE = (
    "src/safe_agent/integrations/google_calendar/provider_simulation.py",
    "src/safe_agent/integrations/google_calendar/acceptance.py",
    "src/safe_agent/integrations/google_calendar/recovery.py",
    "src/safe_agent/integrations/google_calendar/outbox.py",
    "src/safe_agent/integrations/google_calendar/service.py",
    "migrations/versions/0004_tal_office_execution_authorization.py",
    "migrations/versions/0005_google_test_outbox_binding.py",
    "migrations/versions/0006_google_provider_simulation.py",
    "docs/M5_ACCEPTANCE_READINESS.md",
    "docs/M5_RELEASE_GATE.md",
)


class M5ReleaseGateReport(StrictModel):
    """Sanitized deterministic CI report, never an executable authorization."""

    schema_version: Literal["safe_agent.m5_release_gate.v1"] = (
        "safe_agent.m5_release_gate.v1"
    )
    offline_configuration: Literal["CONFIGURED", "INCOMPLETE"]
    live_acceptance: Literal["BLOCKED"] = "BLOCKED"
    production_authorized: Literal[False] = False
    live_google_write_verified: Literal[False] = False
    tal_commit: str | None = Field(default=None, max_length=40)
    problems: tuple[str, ...] = ()


def read_tal_pin(root: Path) -> str:
    """One strict SHA-1 revision for BOTH CI jobs; reject interpolation/injection."""
    raw = (root / PIN_FILE).read_text(encoding="utf-8").strip()
    if re.fullmatch(r"[0-9a-f]{40}", raw) is None:
        raise ValueError("TAL_PIN_NOT_A_40_CHARACTER_HEX_COMMIT")
    return raw


def inspect_m5_release(root: Path) -> M5ReleaseGateReport:
    """Check repository wiring only; caller must run CI and inspect its results."""
    problems: list[str] = []
    try:
        sha = read_tal_pin(root)
    except (ValueError, OSError):
        sha = None
        problems.append("TAL_PIN_INVALID_OR_MISSING")
    for relative in REQUIRED_SOURCE:
        if not (root / relative).is_file():
            problems.append("MISSING_REQUIRED_ARTIFACT:" + relative)
    for path in WORKFLOWS:
        try:
            content = (root / path).read_text(encoding="utf-8")
        except OSError:
            problems.append("MISSING_CI_WORKFLOW:" + path.as_posix())
            continue
        if ("scripts/read_tal_pin.py" not in content
                or TAL_REPOSITORY not in content):
            problems.append("CI_TAL_PIN_NOT_SHARED:" + path.name)
        # TAL tests must NOT be silently skipped because TAL wasn't installed.
        if "git+https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git" not in content:
            problems.append("CI_UPSTREAM_TAL_INSTALL_MISSING:" + path.name)
        for test in REQUIRED_CROSS_REPO_TESTS:
            if test not in content:
                problems.append("CI_TEST_NOT_PINNED:" + path.name + ":" + test)
        # Reject competing hard-coded TAL URLs in the two CI workflows.
        if re.search(
            r"TervoroAgenticLanguage-TAL-\.git@[0-9a-f]{40}", content,
        ):
            problems.append("CI_INLINE_TAL_PIN_DRIFT:" + path.name)
    try:
        ci = (root / WORKFLOWS[0]).read_text(encoding="utf-8")
        if "scripts/check_m5_release.py --mode offline" not in ci:
            problems.append("CI_M5_RELEASE_CHECK_MISSING")
    except OSError:
        pass
    for doc_path, marker in (
        ("README.md", "M5 LIVE ACCEPTANCE: BLOCKED"),
        ("docs/M5_RELEASE_GATE.md", "LIVE ACCEPTANCE: BLOCKED"),
    ):
        try:
            if marker not in (root / doc_path).read_text(encoding="utf-8"):
                problems.append("M5_LIVE_BLOCK_NOT_DOCUMENTED:" + doc_path)
        except OSError:
            problems.append("M5_LIVE_BLOCK_DOC_MISSING:" + doc_path)
    # This report never changes to LIVE_ACCEPTED on local claims. GitHub CI
    # never receives OAuth secrets and cannot authenticate a real booking.
    return M5ReleaseGateReport(
        offline_configuration="CONFIGURED" if not problems else "INCOMPLETE",
        tal_commit=sha,
        problems=tuple(sorted(set(problems))),
    )
