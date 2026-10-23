"""Build, launch, and probe the narrow Linux Docker episode-service profile.

Only this explicit deployment profile has the attempted-read acceptance test.
The ordinary Python sidecar launch does not acquire these Docker restrictions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
SERVICE_TARGET = "/run/safe-episode"
DISPATCH_TARGET = "/run/secrets/dispatch-key"
PROVIDER_TARGET = "/run/secrets/agent-provider-key"
STATE_NAMES = frozenset(
    {
        "dispatch.sqlite",
        "dispatch.sqlite-journal",
        "dispatch.sqlite-wal",
        "dispatch.sqlite-shm",
        "episode.sock",
        "episode.sock.owner",
    }
)


def prepare_context(destination: Path) -> dict[str, str]:
    """Positive file allowlist; no repository-wide COPY or benchmark installation."""
    destination = destination.absolute()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink() or any(destination.iterdir()):
        raise ValueError("BUILD_CONTEXT_MUST_BE_EMPTY")
    sources = json.loads((ROOT / "docker/episode_service.sources.json").read_text())
    hashes = {}
    for relative in sources:
        path = Path(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".py"
            or path.parts[0] != "safe_agent"
            or {"benchmarks", "domains", "tau2"} & set(path.parts)
        ):
            raise ValueError("IMAGE_SOURCE_NOT_ALLOWED")
        source = ROOT / "src" / path
        if source.resolve() != source or not source.is_file():
            raise ValueError("IMAGE_SOURCE_UNSAFE")
        target = destination / path
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o644)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    for source, target in (
        ("episode_service.Dockerfile", "Dockerfile"),
        ("episode_service.requirements.lock", "requirements.lock"),
        ("episode_service_probe.py", "probe.py"),
    ):
        shutil.copyfile(ROOT / "docker" / source, destination / target)
    (destination / "sources.json").write_text(json.dumps(hashes, sort_keys=True) + "\n")
    return hashes


def _owned_path(path: Path, *, directory: bool) -> Path:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or path.is_relative_to(ROOT)
        or any(char in str(path) for char in (",", "\n", "\r", "\x00"))
    ):
        raise ValueError("SERVICE_PATH_MUST_BE_ABSOLUTE_OUTSIDE_REPOSITORY")
    info = path.lstat()
    wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not wanted
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError("SERVICE_PATH_OWNERSHIP_OR_MODE_DENIED")
    return path


def _validate_state_directory(service: Path) -> None:
    for item in service.iterdir():
        info = item.lstat()
        if (
            item.name not in STATE_NAMES
            or item.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not (
                stat.S_ISSOCK(info.st_mode)
                if item.name == "episode.sock"
                else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            )
        ):
            raise ValueError("SERVICE_DIRECTORY_CONTAINS_UNRELATED_DATA")


def run_arguments(
    *,
    image: str,
    service: Path,
    dispatch_key: Path,
    provider_key: Path,
    model: str,
    name: str,
    provider_network: bool = False,
) -> list[str]:
    """No arbitrary mounts, environment passthrough, runtime flags, or entrypoint."""
    if os.getuid() == 0 or os.getgid() == 0:
        raise ValueError("RUN_FROM_A_NONROOT_LINUX_ACCOUNT")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("EXACT_LOCAL_IMAGE_ID_REQUIRED")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", model) or not re.fullmatch(
        r"safe-episode-[a-z0-9-]{1,48}", name
    ):
        raise ValueError("FIXED_MODEL_AND_CONTAINER_ID_REQUIRED")
    service = _owned_path(service, directory=True)
    dispatch_key = _owned_path(dispatch_key, directory=False)
    provider_key = _owned_path(provider_key, directory=False)
    if (
        dispatch_key == provider_key
        or dispatch_key.is_relative_to(service)
        or provider_key.is_relative_to(service)
    ):
        raise ValueError("DEDICATED_DISTINCT_KEYS_REQUIRED")
    _validate_state_directory(service)
    return [
        *DOCKER,
        "run",
        "--detach",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--workdir",
        "/",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--ipc=private",
        "--pids-limit=64",
        "--memory=512m",
        "--cpus=1",
        "--network=" + ("bridge" if provider_network else "none"),
        "--mount",
        f"type=bind,source={service},target={SERVICE_TARGET}",
        "--mount",
        f"type=bind,source={dispatch_key},target={DISPATCH_TARGET},readonly",
        "--mount",
        f"type=bind,source={provider_key},target={PROVIDER_TARGET},readonly",
        "--env",
        f"SAFE_EPISODE_SERVICE_DIR={SERVICE_TARGET}",
        "--env",
        f"SAFE_EPISODE_DISPATCH_KEY_FILE={DISPATCH_TARGET}",
        "--env",
        f"SAFE_EPISODE_OPENAI_KEY_FILE={PROVIDER_TARGET}",
        "--env",
        f"SAFE_EPISODE_MODEL={model}",
        image,
    ]


def _docker(
    arguments: list[str], *, body: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    # No inherited provider, evaluator, proxy, Docker-host, or Python environment.
    return subprocess.run(
        arguments,
        input=body,
        text=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=45,
        check=check,
    )


def _http(socket_path: Path, method: str, route: str, body: str = "") -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(socket_path))
        request = (
            f"{method} {route} HTTP/1.1\r\nHost: safe-episode\r\n"
            "Connection: close\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body.encode())}\r\n\r\n{body}"
        )
        connection.sendall(request.encode())
        result = bytearray()
        while chunk := connection.recv(4096):
            result.extend(chunk)
            if len(result) > 16_384:
                raise RuntimeError("UNEXPECTED_SERVICE_RESPONSE_SIZE")
        return bytes(result)


def probe(image: str) -> dict[str, object]:
    """Boot the real service, then attempt forbidden reads inside its container."""
    if not __debug__:
        raise ValueError("PROBE_REQUIRES_ACTIVE_ASSERTIONS")
    if os.getuid() == 0:
        raise ValueError("RUN_FROM_A_NONROOT_LINUX_ACCOUNT")
    name = "safe-episode-probe-" + uuid4().hex
    with tempfile.TemporaryDirectory(prefix="safe-episode-host-") as temporary:
        root = Path(temporary)
        service = root / "service"
        service.mkdir(mode=0o700)
        dispatch, provider = root / "dispatch-key", root / "agent-key"
        for path, marker in ((dispatch, "synthetic-dispatch-"), (provider, "synthetic-provider-")):
            path.write_text(marker + uuid4().hex)
            path.chmod(0o600)
        forbidden = [root / "evaluator-answers.json", root / "harness-execution-secret"]
        for path in forbidden:
            path.write_text("unmounted-host-canary-" + uuid4().hex)
            path.chmod(0o600)
            assert path.read_text().startswith("unmounted-host-canary-")
        # These values exist in the launcher host, but are never forwarded to Docker.
        previous = {
            key: os.environ.get(key)
            for key in ("OPENAI_API_KEY", "SAFE_TEST_HARNESS_SECRET", "TAU2_DATA_DIR")
        }
        for key in previous:
            os.environ[key] = "unforwarded-host-environment-" + uuid4().hex
        try:
            _docker(
                run_arguments(
                    image=image,
                    service=service,
                    dispatch_key=dispatch,
                    provider_key=provider,
                    model="offline_probe",
                    name=name,
                )
            )
            inspected = json.loads(_docker([*DOCKER, "inspect", name]).stdout)[0]
            config = inspected["HostConfig"]
            assert config["ReadonlyRootfs"] and not config["Privileged"]
            assert config["NetworkMode"] == "none" and config["PidMode"] != "host"
            assert config["CapDrop"] == ["ALL"]
            assert {"no-new-privileges", "no-new-privileges:true"} & set(config["SecurityOpt"])
            assert inspected["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
            assert {(m["Source"], m["Destination"], m["RW"]) for m in inspected["Mounts"]} == {
                (str(service), SERVICE_TARGET, True),
                (str(dispatch), DISPATCH_TARGET, False),
                (str(provider), PROVIDER_TARGET, False),
            }
            deadline = time.monotonic() + 30
            while True:
                try:
                    health = _http(service / "episode.sock", "GET", "/healthz")
                    assert health.split(b"\r\n", 1)[0].endswith(b"200 OK")
                    assert json.loads(health.split(b"\r\n\r\n", 1)[1]) == {
                        "status": "ready",
                        "mode": "episode_model_only",
                    }
                    break
                except (OSError, AssertionError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("SERVICE_HEALTH_NOT_READY") from None
                    time.sleep(0.1)
            denied = _http(service / "episode.sock", "POST", "/v1/episode/infer", "{}")
            assert denied.split(b"\r\n", 1)[0].endswith(b"403 Forbidden")
            result = _docker(
                [*DOCKER, "exec", "-i", name, "python", "-I", "-B", "/opt/episode/probe.py"],
                body=json.dumps({"forbidden_paths": list(map(str, forbidden))}),
            )
            report = json.loads(result.stdout)
            assert report["status"] == "passed" and report["denied_read_attempts"] == 6
            with sqlite3.connect(f"file:{service / 'dispatch.sqlite'}?mode=ro", uri=True) as db:
                assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
            return {
                **report,
                "image_id": image,
                "service_health_verified": True,
                "unauthenticated_inference_denied": True,
                "provider_dispatches": 0,
            }
        finally:
            _docker([*DOCKER, "rm", "-f", name], check=False)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    context = commands.add_parser("prepare-context")
    context.add_argument("--output", required=True, type=Path)
    acceptance = commands.add_parser("probe")
    acceptance.add_argument("--image", required=True)
    acceptance.add_argument("--output", type=Path)
    launch = commands.add_parser("run")
    launch.add_argument("--image", required=True)
    launch.add_argument("--service-dir", required=True, type=Path)
    launch.add_argument("--dispatch-key", required=True, type=Path)
    launch.add_argument("--provider-key", required=True, type=Path)
    launch.add_argument("--model", required=True)
    launch.add_argument(
        "--provider-network",
        action="store_true",
        help="Enable bridge networking for a deliberately configured hosted run",
    )
    args = parser.parse_args()
    if args.command == "prepare-context":
        print(json.dumps({"source_files": len(prepare_context(args.output))}))
    elif args.command == "probe":
        report = json.dumps(probe(args.image), indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.write_text(report)
        print(report, end="")
    else:
        name = "safe-episode-service-" + uuid4().hex
        result = _docker(
            run_arguments(
                image=args.image,
                service=args.service_dir,
                dispatch_key=args.dispatch_key,
                provider_key=args.provider_key,
                model=args.model,
                name=name,
                provider_network=args.provider_network,
            )
        )
        print(
            json.dumps(
                {
                    "container_id": result.stdout.strip(),
                    "name": name,
                    "socket": str(args.service_dir / "episode.sock"),
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("EPISODE_CONTAINER_PROFILE_FAILED", file=sys.stderr)
        raise SystemExit(1) from None
