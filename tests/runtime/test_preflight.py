"""Read-only environment diagnostics; unit cases use explicit metadata doubles."""
from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from safe_agent.runtime import preflight
from safe_agent.runtime.preflight import Diagnostic, InstalledPackage, check_metadata
from safe_agent.runtime.profile import STEM, TARGET, ProfileInvalid, load_profile, parse_profile


@pytest.fixture
def profile():
    return load_profile()


@pytest.fixture
def inventory(profile):
    rows = [InstalledPackage(pin.name, pin.version) for pin in profile.packages]
    rows = [replace(row, requires=("psycopg-binary==3.3.6; extra == 'binary'",), extras=("binary",))
            if row.name == "psycopg" else row for row in rows]
    rows.extend([
        InstalledPackage("tervoro-safe-agent", "0.1.0", (
            "cryptography==45.0.7; extra == 'coordinated-offline'",
            "cryptography==46.0.4; extra == 'google-test'",
            "psycopg[binary]>=3.2; extra == 'postgres'",
        ), ("dev", "api", "postgres", "coordinated-offline", "google-test")),
        InstalledPackage("tervoro-agentic-language", "0.4.0rc2", ("cryptography>=43,<46",)),
    ])
    return tuple(rows)


def configure(monkeypatch, profile, inventory):
    monkeypatch.setattr(preflight, "load_profile", lambda: profile)
    monkeypatch.setattr(preflight, "collect_inventory", lambda: inventory)
    monkeypatch.setattr(preflight, "current_target", lambda: dict(TARGET))
    monkeypatch.setattr(preflight.sys, "prefix", "/synthetic/isolated-prefix")
    monkeypatch.setattr(preflight, "_verify_tal", lambda _: None)


def test_healthy_metadata_accepts_selected_extras_not_legacy_google(profile, inventory):
    assert check_metadata(profile, inventory) == set()


@pytest.mark.parametrize("name", ["cryptography", "pydantic", "pip", "setuptools", "psycopg-binary"])
def test_each_missing_package_blocks(profile, inventory, name):
    missing = tuple(row for row in inventory if row.name != name)
    assert Diagnostic("PACKAGE_MISSING", name) in check_metadata(profile, missing)


@pytest.mark.parametrize("name", ["cryptography", "httpx", "packaging", "tervoro-agentic-language"])
def test_version_change_never_silently_passes(profile, inventory, name):
    changed = tuple(replace(row, version="999.0") if row.name == name else row for row in inventory)
    assert Diagnostic("PACKAGE_VERSION_MISMATCH", name) in check_metadata(profile, changed)


def test_duplicate_distribution_denied(profile, inventory):
    assert Diagnostic("DUPLICATE_DISTRIBUTION", inventory[0].name) in check_metadata(profile, inventory + inventory[:1])


def test_unlocked_distribution_is_not_printed(profile, inventory):
    issues = check_metadata(profile, inventory + (InstalledPackage("private-customer-name", "1"),))
    assert issues == {Diagnostic("UNLOCKED_DISTRIBUTION")}
    assert "private-customer" not in str(issues)


def test_declared_dependency_conflict_blocks(profile, inventory):
    changed = tuple(replace(row, requires=("cryptography>=46",))
                    if row.name == "tervoro-agentic-language" else row for row in inventory)
    assert Diagnostic("DEPENDENCY_CONFLICT", "tervoro-agentic-language") in check_metadata(profile, changed)


def test_missing_selected_extra_blocks(profile, inventory):
    changed = tuple(replace(row, extras=()) if row.name == "psycopg" else row for row in inventory)
    assert Diagnostic("REQUIRED_EXTRA_MISSING", "psycopg") in check_metadata(profile, changed)


@pytest.mark.parametrize("requirement", ["unlocked-library>=1", "httpx @ https://untrusted.invalid/a.whl"])
def test_unlocked_or_url_dependency_blocks(profile, inventory, requirement):
    changed = tuple(replace(row, requires=(requirement,)) if row.name == "httpcore" else row for row in inventory)
    assert Diagnostic("DEPENDENCY_NOT_LOCKED", "httpcore") in check_metadata(profile, changed)


def test_malformed_metadata_has_fixed_diagnostic(profile, inventory):
    changed = tuple(replace(row, requires=("SECRET_EXCEPTION_DATA !!!",))
                    if row.name == "httpcore" else row for row in inventory)
    issues = check_metadata(profile, changed)
    assert Diagnostic("DEPENDENCY_METADATA_INVALID") in issues
    assert "SECRET_EXCEPTION_DATA" not in str(issues)


def test_preflight_is_read_only_and_report_is_not_authority(monkeypatch, profile, inventory):
    configure(monkeypatch, profile, inventory)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("forbidden")
        raise AssertionError("no network or subprocess in preflight")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    report = preflight.inspect_environment()
    assert report.status == "PASS" and calls == []
    data = asdict(report)
    assert data["scope"] == "DEPENDENCY_ENVIRONMENT_ONLY"
    for field in ("production_authorized", "live_google_write_verified",
                  "hosted_inference_enabled", "business_execution_authorized"):
        assert data[field] is False


@pytest.mark.parametrize("key,value", [("python", "3.13"), ("system", "Windows"),
                                       ("machine", "aarch64"), ("libc", "musl")])
def test_unsupported_target_blocks(monkeypatch, profile, inventory, key, value):
    configure(monkeypatch, profile, inventory)
    monkeypatch.setattr(preflight, "current_target", lambda: {**TARGET, key: value})
    assert Diagnostic("PLATFORM_UNSUPPORTED") in preflight.inspect_environment().diagnostics


def test_nonisolated_environment_blocks(monkeypatch, profile, inventory):
    configure(monkeypatch, profile, inventory)
    monkeypatch.setattr(preflight.sys, "prefix", preflight.sys.base_prefix)
    assert Diagnostic("ISOLATED_ENVIRONMENT_REQUIRED") in preflight.inspect_environment().diagnostics


def test_bad_versions_do_not_import_tal(monkeypatch, profile, inventory):
    configure(monkeypatch, profile, inventory[:-1])
    calls = []
    monkeypatch.setattr(preflight, "_verify_tal", lambda _: calls.append("imported"))
    assert preflight.inspect_environment().status == "BLOCKED"
    assert calls == []


def test_tal_provenance_failure_blocks_with_no_raw_error(monkeypatch, profile, inventory):
    configure(monkeypatch, profile, inventory)
    def broken(_):
        raise RuntimeError("secret token or private path must not be printed")
    monkeypatch.setattr(preflight, "_verify_tal", broken)
    report = preflight.inspect_environment()
    assert Diagnostic("TAL_PROVENANCE_UNVERIFIED") in report.diagnostics
    assert "secret token" not in json.dumps(asdict(report))


def test_cli_exit_status_and_safe_exception(capsys):
    def broken():
        raise RuntimeError("PRIVATE_DIAGNOSTIC_PAYLOAD")
    assert preflight.main([], inspect=broken) == 2
    output = capsys.readouterr().out
    assert "PRIVATE_DIAGNOSTIC_PAYLOAD" not in output
    assert json.loads(output)["status"] == "BLOCKED"


def profile_bytes():
    import safe_agent.runtime.profile as module
    folder = Path(module.__file__).parent
    return (folder / (STEM + ".json")).read_bytes(), (folder / (STEM + ".lock")).read_bytes()


@pytest.mark.parametrize("mutation", ["duplicate_json", "extra_field", "wrong_target", "missing_package", "duplicate_package",
                                     "invalid_hash", "lock_directive", "lock_duplicate", "too_large"])
def test_closed_profile_and_hash_lock_fail_closed(mutation):
    raw, lock = profile_bytes()
    data = json.loads(raw)
    if mutation == "duplicate_json":
        raw = raw.replace(b'"schema_version":', b'"schema_version":"duplicate","schema_version":', 1)
    elif mutation == "extra_field":
        data["production_authorized"] = True
    elif mutation == "wrong_target":
        data["target"]["python"] = "3.13"
    elif mutation == "missing_package":
        data["packages"] = data["packages"][1:]
    elif mutation == "duplicate_package":
        data["packages"].append(data["packages"][0])
    elif mutation == "invalid_hash":
        data["packages"][0]["sha256"] = "invalid"
    elif mutation == "lock_directive":
        lock += b"--extra-index-url https://untrusted.invalid\n"
    elif mutation == "lock_duplicate":
        lock += next(line for line in lock.splitlines(keepends=True) if line.startswith(b"httpx=="))
    elif mutation == "too_large":
        raw = b" " * 131073
    if mutation not in {"duplicate_json", "too_large"}:
        raw = json.dumps(data).encode()
    with pytest.raises(ProfileInvalid):
        parse_profile(raw, lock)
