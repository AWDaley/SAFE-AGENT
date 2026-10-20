"""Attempted-read acceptance inside the running, network-disabled service image.

Input contains forbidden host paths, never their canary values. The synthetic
provider/dispatch files are deliberately readable; they belong to this service.
This is a finite deployment probe, not a generic hostile-code containment proof.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path


def require(value: bool, code: str) -> None:
    if not value:
        raise RuntimeError(code)


def denied_read(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            stream.read(1)
    except OSError as exc:
        require(exc.errno in {errno.ENOENT, errno.EACCES, errno.ENOTDIR}, "UNEXPECTED_READ_ERROR")
    else:
        raise RuntimeError("FORBIDDEN_HOST_FILE_READABLE")


def main() -> None:
    payload = json.loads(sys.stdin.buffer.read(16385))
    require(
        isinstance(payload, dict) and set(payload) == {"forbidden_paths"}, "INVALID_PROBE_INPUT"
    )
    paths = payload["forbidden_paths"]
    require(isinstance(paths, list) and 2 <= len(paths) <= 8, "INVALID_PROBE_PATHS")
    require(os.getuid() != 0 and os.geteuid() != 0, "ROOT_PROCESS")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    require(status["NoNewPrivs"].strip() == "1", "NO_NEW_PRIVILEGES_MISSING")
    for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        require(int(status[name].strip(), 16) == 0, "CAPABILITIES_REMAIN")
    require(bool(os.statvfs("/").f_flag & os.ST_RDONLY), "ROOT_FILESYSTEM_WRITABLE")
    require(not Path("/var/run/docker.sock").exists(), "DOCKER_SOCKET_EXPOSED")
    require(importlib.util.find_spec("tau2") is None, "TAU_PACKAGE_EXPOSED")

    import safe_agent
    from safe_agent.inference_service.__main__ import _secret_from_file

    package = Path(safe_agent.__file__).parent
    require(not (package / "benchmarks").exists(), "EVALUATOR_CODE_EXPOSED")
    require(not (package / "domains").exists(), "DOMAIN_CATALOG_EXPOSED")
    source_hashes = json.loads(Path("/opt/episode/sources.json").read_text())
    require(
        {str(path.relative_to(package.parent)) for path in package.rglob("*.py")}
        == set(source_hashes),
        "IMAGE_SOURCE_ALLOWLIST_CHANGED",
    )
    for name, digest in source_hashes.items():
        require(
            hashlib.sha256((package.parent / name).read_bytes()).hexdigest() == digest,
            "IMAGE_SOURCE_BYTES_CHANGED",
        )
    for name in ("SAFE_EPISODE_DISPATCH_KEY_FILE", "SAFE_EPISODE_OPENAI_KEY_FILE"):
        path = Path(os.environ[name])
        require(len(_secret_from_file(path).get_secret_value()) >= 32, "DEDICATED_KEY_UNREADABLE")
        require(stat.S_ISREG(path.stat().st_mode), "KEY_NOT_REGULAR")
        try:
            descriptor = os.open(path, os.O_WRONLY)
        except OSError as exc:
            require(exc.errno == errno.EROFS, "KEY_MOUNT_NOT_READ_ONLY")
        else:
            os.close(descriptor)
            raise RuntimeError("KEY_MOUNT_WRITABLE")
    for name in ("OPENAI_API_KEY", "SAFE_TEST_HARNESS_SECRET", "TAU2_DATA_DIR"):
        require(name not in os.environ, "HOST_ENVIRONMENT_INHERITED")
        require(
            name.encode() + b"=" not in Path("/proc/1/environ").read_bytes(),
            "SERVICE_INHERITED_HOST_ENVIRONMENT",
        )
    for raw in paths:
        require(isinstance(raw, str) and Path(raw).is_absolute(), "INVALID_PROBE_PATH")
        for prefix in ("", "/proc/1/root", "/proc/self/root"):
            denied_read(Path(prefix + raw))
    print(
        json.dumps(
            {
                "status": "passed",
                "forbidden_host_files": len(paths),
                "denied_read_attempts": len(paths) * 3,
                "dedicated_key_reads": 2,
                "provider_calls": 0,
                "network_profile": "none",
                "scope": "specific container image, mounts, environment, and process flags",
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("EPISODE_CONTAINER_PROBE_FAILED", file=sys.stderr)
        raise SystemExit(1) from None
