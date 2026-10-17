"""Resolver and installer safeguards; these unit tests never download packages."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from safe_agent.runtime.profile import load_profile

ROOT = Path(__file__).resolve().parents[2]


def script(name):
    spec = importlib.util.spec_from_file_location("tested_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("line", ["pydantic>=2", "-r alternate.txt", "--extra-index-url https://untrusted.invalid",
                                  "pydantic @ https://untrusted.invalid/a.whl", "pydantic==2.*", ""])
def test_resolver_rejects_unpinned_or_directive_inputs(line):
    with pytest.raises(ValueError):
        script("resolve_coordinated_profile").input_roots(line)


def test_resolver_duplicate_canonical_names_denied():
    with pytest.raises(ValueError):
        script("resolve_coordinated_profile").input_roots("my_lib==1\nmy-lib==1\n")


def test_committed_lock_matches_resolver_inputs_and_pin():
    resolver = script("resolve_coordinated_profile")
    profile = load_profile()
    raw = resolver.INPUT.read_bytes()
    assert profile.inputs_sha256 == hashlib.sha256(raw).hexdigest()
    assert dict(profile.roots) == resolver.input_roots(raw.decode())
    value = json.loads(resolver.PROFILE.read_text())
    assert resolver.LOCK.read_text() == resolver.render_lock(value)
    assert profile.tal_commit == (ROOT / "ci/tal-office-commit.sha").read_text().strip()


@pytest.mark.parametrize("mutation", ["url", "source_archive", "missing_hash", "root_drift", "duplicate"])
def test_resolver_rejects_unreviewable_artifacts(mutation):
    resolver = script("resolve_coordinated_profile")
    entry = {"metadata": {"name": "pip", "version": "25.2"}, "download_info": {
        "url": "https://files.pythonhosted.org/packages/pip-25.2-py3-none-any.whl",
        "archive_info": {"hashes": {"sha256": "a" * 64}},
    }}
    report = {"install": [entry]}
    if mutation == "url":
        entry["download_info"]["url"] = "https://untrusted.invalid/pip.whl"
    elif mutation == "source_archive":
        entry["download_info"]["url"] = "https://files.pythonhosted.org/pip.tar.gz"
    elif mutation == "missing_hash":
        entry["download_info"]["archive_info"] = {}
    elif mutation == "root_drift":
        entry["metadata"]["version"] = "25.3"
    elif mutation == "duplicate":
        report["install"].append(entry)
    with pytest.raises(ValueError):
        resolver.profile_from_report(report, b"pip==25.2\n", "a" * 40)


def test_installer_refuses_existing_directory_and_preserves_contents(tmp_path, monkeypatch):
    installer = script("install_coordinated_runtime")
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("do not delete")
    monkeypatch.setattr(installer.venv.EnvBuilder, "create", lambda *_: pytest.fail("must not touch existing environment"))
    with pytest.raises(FileExistsError):
        installer.create_environment(destination)
    assert marker.read_text() == "do not delete"


def test_installer_refuses_symlink_destination(tmp_path, monkeypatch):
    installer = script("install_coordinated_runtime")
    original = tmp_path / "original"
    original.mkdir()
    destination = tmp_path / "symlink"
    destination.symlink_to(original, target_is_directory=True)
    monkeypatch.setattr(installer.venv.EnvBuilder, "create", lambda *_: pytest.fail("must not follow destination symlink"))
    with pytest.raises(FileExistsError):
        installer.create_environment(destination)
    assert list(original.iterdir()) == []


def test_installer_rejects_platform_before_creating_or_downloading(tmp_path, monkeypatch):
    installer = script("install_coordinated_runtime")
    monkeypatch.setattr(installer.platform, "system", lambda: "unsupported")
    monkeypatch.setattr(installer.subprocess, "run", lambda *_a, **_k: pytest.fail("no download"))
    destination = tmp_path / "not-created"
    with pytest.raises(ValueError):
        installer.install(destination)
    assert not destination.exists()
