"""Provenance checks use metadata fixtures; no installs, paid calls, or dispatch."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from safe_agent.domains import get_domain_pack
from safe_agent.tal import domain_runtime


def install_fixture(tmp_path, monkeypatch):
    commit = "a" * 40
    monkeypatch.setattr(domain_runtime, "REVIEWED_TAL_BENCHMARK_COMMIT", commit)
    hashes = {}
    modules = {}
    for name in ("__init__", "core", "domain_admission"):
        path = tmp_path / "tal" / (name + ".py")
        path.parent.mkdir(exist_ok=True)
        path.write_text("# authored provenance fixture " + name + "\n")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        modules["tal" if name == "__init__" else "tal." + name] = SimpleNamespace(
            __file__=str(path)
        )
    direct = {
        "url": domain_runtime.REVIEWED_TAL_REPOSITORY,
        "vcs_info": {"vcs": "git", "commit_id": commit},
    }
    installed = SimpleNamespace(
        read_text=lambda _: json.dumps(direct), locate_file=lambda path: tmp_path / path
    )
    monkeypatch.setattr(domain_runtime, "REVIEWED_TAL_MODULE_SHA256", hashes)
    monkeypatch.setattr(domain_runtime, "distribution", lambda _: installed)
    monkeypatch.setattr(domain_runtime, "import_module", lambda name: modules[name])
    return direct, modules


def test_reviewed_commit_origin_and_bytes_are_all_required(tmp_path, monkeypatch):
    direct, modules = install_fixture(tmp_path, monkeypatch)
    domain_runtime.require_reviewed_domain_runtime()
    direct["vcs_info"]["commit_id"] = "b" * 40
    with pytest.raises(domain_runtime.TALDomainRuntimeDenied):
        domain_runtime.require_reviewed_domain_runtime()
    direct["vcs_info"]["commit_id"] = "a" * 40
    path = Path(modules["tal.domain_admission"].__file__)
    path.write_text("# changed code at correct installed path\n")
    with pytest.raises(domain_runtime.TALDomainRuntimeDenied):
        domain_runtime.require_reviewed_domain_runtime()


def test_same_named_shadow_and_editable_install_denied(tmp_path, monkeypatch):
    direct, modules = install_fixture(tmp_path, monkeypatch)
    direct["dir_info"] = {"editable": True}
    with pytest.raises(domain_runtime.TALDomainRuntimeDenied):
        domain_runtime.require_reviewed_domain_runtime()
    del direct["dir_info"]
    actual = Path(modules["tal.core"].__file__)
    shadow = tmp_path / "shadow.py"
    shadow.write_bytes(actual.read_bytes())
    modules["tal.core"].__file__ = str(shadow)
    with pytest.raises(domain_runtime.TALDomainRuntimeDenied):
        domain_runtime.require_reviewed_domain_runtime()


def test_no_environment_bypass_or_unpinned_default_pack(tmp_path, monkeypatch):
    install_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(domain_runtime, "REVIEWED_TAL_BENCHMARK_COMMIT", "PENDING")
    monkeypatch.setenv("SAFE_AGENT_SKIP_TAL_PROVENANCE", "1")
    with pytest.raises(
        domain_runtime.TALDomainRuntimeDenied, match="TAL_REVIEWED_DOMAIN_RUNTIME_REQUIRED"
    ):
        get_domain_pack("retail")


def test_checked_in_module_hashes_and_separate_pin_are_consistent():
    module = pytest.importorskip(
        "tal.domain_admission", reason="New benchmark TAL is optional in office-only CI"
    )
    tal_root = Path(module.__file__).parent
    for name, expected in domain_runtime.REVIEWED_TAL_MODULE_SHA256.items():
        assert hashlib.sha256((tal_root / (name + ".py")).read_bytes()).hexdigest() == expected
    repo = Path(__file__).resolve().parents[2]
    assert (
        repo / "ci/tal-benchmark-commit.sha"
    ).read_text().strip() == domain_runtime.REVIEWED_TAL_BENCHMARK_COMMIT
    assert (
        repo / "ci/tal-office-commit.sha"
    ).read_text().strip() != domain_runtime.REVIEWED_TAL_BENCHMARK_COMMIT
