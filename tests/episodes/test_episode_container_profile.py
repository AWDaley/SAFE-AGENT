"""Free build-context guards; the actual attempted-read probe is mandatory in CI."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import episode_service_container as profile


def test_model_image_context_contains_only_the_reviewed_source_allowlist(tmp_path):
    hashes = profile.prepare_context(tmp_path / "context")
    copied = tmp_path / "context/safe_agent"
    assert len(hashes) > 0
    assert not (copied / "benchmarks").exists()
    assert not (copied / "domains").exists()
    assert not (tmp_path / "context/tests").exists()
    assert not (tmp_path / "context/data").exists()
    assert set(hashes) == {
        str(path.relative_to(tmp_path / "context")) for path in copied.rglob("*.py")
    }
    for name, digest in hashes.items():
        assert hashlib.sha256((tmp_path / "context" / name).read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match="BUILD_CONTEXT_MUST_BE_EMPTY"):
        profile.prepare_context(tmp_path / "context")


@pytest.mark.parametrize(
    "source",
    [
        "../private.py",
        "/private.py",
        "safe_agent/benchmarks/evaluator.py",
        "safe_agent/domains/catalog.py",
        "tau2/secret.py",
    ],
)
def test_image_source_allowlist_rejects_forbidden_paths(monkeypatch, tmp_path, source):
    repository = tmp_path / "repository"
    (repository / "docker").mkdir(parents=True)
    (repository / "docker/episode_service.sources.json").write_text(json.dumps([source]))
    monkeypatch.setattr(profile, "ROOT", repository)
    with pytest.raises(ValueError, match="IMAGE_SOURCE_NOT_ALLOWED"):
        profile.prepare_context(tmp_path / "context")


def test_image_source_symlink_cannot_copy_a_host_secret(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    (repository / "docker").mkdir(parents=True)
    (repository / "src/safe_agent").mkdir(parents=True)
    secret = tmp_path / "host-secret.py"
    secret.write_text("HOST_SECRET_CANARY")
    (repository / "src/safe_agent/__init__.py").symlink_to(secret)
    (repository / "docker/episode_service.sources.json").write_text(
        json.dumps(["safe_agent/__init__.py"])
    )
    monkeypatch.setattr(profile, "ROOT", repository)
    with pytest.raises(ValueError, match="IMAGE_SOURCE_UNSAFE"):
        profile.prepare_context(tmp_path / "context")
    assert not (tmp_path / "context/safe_agent/__init__.py").exists()


def test_docker_process_does_not_inherit_host_credentials_or_remote_daemon(monkeypatch):
    observed = {}
    monkeypatch.setenv("OPENAI_API_KEY", "HOST_PROVIDER_CANARY")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.invalid:2375")

    def run(arguments, **kwargs):
        observed.update(kwargs)
        observed["arguments"] = arguments
        return SimpleNamespace(stdout="{}", returncode=0)

    monkeypatch.setattr(profile.subprocess, "run", run)
    profile._docker([*profile.DOCKER, "inspect", "authored_fixture"])
    assert observed["env"] == {"PATH": "/usr/bin:/bin"}
    assert observed["arguments"][:3] == [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
    ]
    assert "HOST_PROVIDER_CANARY" not in str(observed)


def test_root_account_cannot_launch_the_profile(monkeypatch):
    monkeypatch.setattr(profile.os, "getuid", lambda: 0)
    with pytest.raises(ValueError, match="RUN_FROM_A_NONROOT_LINUX_ACCOUNT"):
        profile.run_arguments(
            image="sha256:" + "a" * 64,
            service=Path("/unread"),
            dispatch_key=Path("/unread/key1"),
            provider_key=Path("/unread/key2"),
            model="offline_probe",
            name="safe-episode-fixture",
        )


def test_state_directory_cannot_expose_benchmark_records(tmp_path):
    service = tmp_path / "service"
    service.mkdir(mode=0o700)
    profile._validate_state_directory(service)
    fence = service / "dispatch.sqlite"
    fence.write_bytes(b"synthetic-fence")
    fence.chmod(0o600)
    profile._validate_state_directory(service)
    forbidden = service / "episodes.sqlite"
    forbidden.write_bytes(b"synthetic-private-harness-records")
    forbidden.chmod(0o600)
    with pytest.raises(ValueError, match="SERVICE_DIRECTORY_CONTAINS_UNRELATED_DATA"):
        profile._validate_state_directory(service)


def test_allowed_state_name_cannot_be_a_link_to_private_records(tmp_path):
    secret = tmp_path / "private-harness-records"
    secret.write_bytes(b"synthetic-private-harness-records")
    secret.chmod(0o600)
    service = tmp_path / "service"
    service.mkdir(mode=0o700)
    fence = service / "dispatch.sqlite"
    fence.symlink_to(secret)
    with pytest.raises(ValueError, match="SERVICE_DIRECTORY_CONTAINS_UNRELATED_DATA"):
        profile._validate_state_directory(service)
    fence.unlink()
    fence.hardlink_to(secret)
    with pytest.raises(ValueError, match="SERVICE_DIRECTORY_CONTAINS_UNRELATED_DATA"):
        profile._validate_state_directory(service)
