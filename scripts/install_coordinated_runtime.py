"""Explicit installer for a NEW offline compatibility virtual environment.

May download locked wheels and the reviewed TAL source. Never call from runtime,
operator routes, model tools or recovery. Refuses to overwrite an environment;
a failed partial installation is retained for inspection, never auto-deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require_target() -> None:
    if (sys.version_info[:2] != (3, 12) or platform.python_implementation() != "CPython"
            or platform.system() != "Linux" or platform.machine() != "x86_64"
            or platform.libc_ver()[0] != "glibc"):
        raise ValueError("RUNTIME_INSTALL_TARGET_UNSUPPORTED")


def create_environment(destination: Path) -> Path:
    # mkdir(exist_ok=False) also rejects symlinks and concurrent creators.
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    venv.EnvBuilder(with_pip=True, clear=False, system_site_packages=False).create(destination)
    return destination / "bin/python"


def install(destination: Path) -> None:
    require_target()
    # The bootstrap profile module uses only the standard library. Explicitly
    # resolve this checkout, not a caller-selected PYTHONPATH or working dir.
    sys.path.insert(0, str(ROOT / "src"))
    from safe_agent.runtime.profile import STEM, load_profile

    profile = load_profile()
    raw = (ROOT / "requirements/coordinated-linux-py312.in").read_bytes()
    pin = (ROOT / "ci/tal-office-commit.sha").read_text(encoding="ascii").strip()
    if profile.inputs_sha256 != hashlib.sha256(raw).hexdigest() or profile.tal_commit != pin:
        raise ValueError("RUNTIME_INSTALL_PROFILE_DRIFT")
    if destination.exists() or destination.is_symlink():
        raise ValueError("RUNTIME_INSTALL_DESTINATION_EXISTS")
    destination = destination.absolute()
    python = create_environment(destination)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(destination),
           "PIP_CONFIG_FILE": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"}
    def run(arguments: list[str], *, cwd: Path = ROOT) -> None:
        subprocess.run([str(python), "-I", *arguments], cwd=cwd, env=env,
                       stdin=subprocess.DEVNULL, check=True, timeout=240)
    pip = ["-m", "pip", "--isolated", "--disable-pip-version-check"]
    run(pip + ["install", "--no-input", "--require-hashes", "--only-binary=:all:",
               "--index-url", "https://pypi.org/simple", "--no-cache-dir",
               "-r", str(ROOT / "src/safe_agent/runtime" / (STEM + ".lock")),
               "--report", str(destination / "third-party-install.json")])
    # All declared build/backend dependencies were already wheel/hash pinned.
    # Never let these source installs silently resolve or downgrade libraries.
    run(pip + ["install", "--no-input", "--no-deps", "--no-build-isolation",
               "git+" + profile.tal_repository + "@" + profile.tal_commit])
    run(pip + ["install", "--no-input", "--no-index", "--no-deps", "--no-build-isolation",
               str(ROOT) + "[dev,api,postgres,coordinated-offline]"])
    run(pip + ["check"])
    # This runs against INSTALLED package data outside the source cwd. A report
    # only checks dependencies: it does not approve the source revision or W7.
    run(["-m", "safe_agent.runtime", "--json"], cwd=destination)
    print("COORDINATED_RUNTIME_INSTALL=PASS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv-coordinated")
    args = parser.parse_args()
    try:
        install(args.venv)
    except Exception:
        print("COORDINATED_RUNTIME_INSTALL=BLOCKED; inspect the step output; no automatic fallback", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
