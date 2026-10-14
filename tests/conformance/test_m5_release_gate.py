"""PR5 conformance: stable TAL source pin and fail-closed M5 release status."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from safe_agent.release_gate import (
    REQUIRED_SOURCE,
    WORKFLOWS,
    M5ReleaseGateReport,
    inspect_m5_release,
    read_tal_pin,
)
from scripts.check_m5_release import main

ROOT = Path(__file__).resolve().parents[2]


def test_offline_ci_source_wiring_is_present_but_not_live_certification() -> None:
    result = inspect_m5_release(ROOT)
    assert result.offline_configuration == "CONFIGURED", result.problems
    assert result.problems == ()
    assert result.tal_commit == "e9d09ef47c0e0e18f3955f9c58f7a8941baf6c38"
    assert result.live_acceptance == "BLOCKED"
    assert result.production_authorized is False
    assert result.live_google_write_verified is False


def test_both_workflows_consume_same_exact_upstream_revision() -> None:
    pin = read_tal_pin(ROOT)
    assert len(pin) == 40
    for workflow in WORKFLOWS:
        contents = (ROOT / workflow).read_text(encoding="utf-8")
        assert "scripts/read_tal_pin.py" in contents
        assert "TervoroAgenticLanguage-TAL-.git" in contents
        assert pin not in contents  # no hidden divergent inline pin


@pytest.mark.parametrize("invalid", [
    "", "a" * 39, "g" * 40,
    "a" * 40 + "$(touch /tmp/not_allowed)",
    "refs/heads/main", "abc\n" + "b" * 40,
])
def test_tal_pin_is_never_interpolated_unvalidated(
    invalid: str, tmp_path: Path,
) -> None:
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci/tal-office-commit.sha").write_text(invalid)
    with pytest.raises(ValueError):
        read_tal_pin(tmp_path)


def test_missing_artifact_fails_offline_structure_gate(tmp_path: Path) -> None:
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci/tal-office-commit.sha").write_text(read_tal_pin(ROOT))
    # Minimal temporary copy: config exists, but required artifacts do not.
    result = inspect_m5_release(tmp_path)
    assert result.offline_configuration == "INCOMPLETE"
    assert any("MISSING_REQUIRED_ARTIFACT:" in x for x in result.problems)
    assert result.live_acceptance == "BLOCKED"
    assert main(["--mode", "offline"], root=tmp_path) == 1


def test_bad_ci_pin_and_missing_cross_repo_test_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci/tal-office-commit.sha").write_text("HEAD")
    for rel in REQUIRED_SOURCE:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("placeholder")
    for w in WORKFLOWS:
        dst = tmp_path / w
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("not checked-in conformance")
    (tmp_path / "README.md").write_text("M5 LIVE ACCEPTANCE: BLOCKED")
    (tmp_path / "docs/M5_RELEASE_GATE.md").write_text("LIVE ACCEPTANCE: BLOCKED")
    report = inspect_m5_release(tmp_path)
    assert report.offline_configuration == "INCOMPLETE"
    assert "TAL_PIN_INVALID_OR_MISSING" in report.problems
    assert any("CI_TEST_NOT_PINNED" in p for p in report.problems)


def test_even_clean_offline_config_cannot_release_live(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--mode", "offline"], root=ROOT) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["offline_configuration"] == "CONFIGURED"
    assert ready["live_acceptance"] == "BLOCKED"
    assert main(["--mode", "live"], root=ROOT) == 2
    live = json.loads(capsys.readouterr().out)
    assert live["live_acceptance"] == "BLOCKED"
    assert live["live_google_write_verified"] is False


def test_external_claims_cannot_silently_change_release_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("SAFE_AGENT_LIVE_ACCEPTED", "true")
    monkeypatch.setenv("SAFE_AGENT_ENABLE_GOOGLE_WRITES", "1")
    (tmp_path / "m5-acceptance-report.json").write_text(
        json.dumps({"live_acceptance": "VERIFIED", "production_authorized": True})
    )
    report = inspect_m5_release(ROOT)
    assert report.live_acceptance == "BLOCKED"
    assert report.live_google_write_verified is False
    with pytest.raises(ValidationError):
        M5ReleaseGateReport.model_validate({
            **report.model_dump(),
            "live_acceptance": "VERIFIED",
        })
    with pytest.raises(ValidationError):
        M5ReleaseGateReport.model_validate({
            **report.model_dump(),
            "production_authorized": True,
        })
