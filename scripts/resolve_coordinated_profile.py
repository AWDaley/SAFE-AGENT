"""Explicit maintenance command: resolve a target-specific offline runtime lock.

Resolution may access PyPI. Runtime preflight never invokes this command. It
only writes checkout-local candidate files; it cannot publish or enable a route.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "requirements/coordinated-linux-py312.in"
DEST = ROOT / "src/safe_agent/runtime"
PROFILE = DEST / "coordinated-linux-py312.json"
LOCK = DEST / "coordinated-linux-py312.lock"
TAL_REPOSITORY = "https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git"


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def input_roots(raw: str) -> dict[str, str]:
    roots: dict[str, str] = {}
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[a-z0-9,-]+\])?==([A-Za-z0-9.]+)", text)
        if match is None or canonical(match[1]) in roots:
            raise ValueError("PROFILE_INPUT_NOT_EXACTLY_PINNED")
        roots[canonical(match[1])] = match[2]
    if not roots:
        raise ValueError("PROFILE_INPUT_EMPTY")
    return roots


def render_lock(value: dict) -> str:
    header = (
        "# Generated third-party wheel lock. CPython 3.12 / Linux x86_64 / glibc only.\n"
        "# Regenerate explicitly with scripts/resolve_coordinated_profile.py --resolve.\n"
        "# TAL and SAFE source are installed separately with --no-deps --no-build-isolation.\n"
        "# This lock does not authorize live providers or certify vulnerability status.\n"
    )
    return header + "".join(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
        for item in value["packages"]
    )


def profile_from_report(report: dict, raw: bytes, tal_commit: str) -> dict:
    roots = input_roots(raw.decode("utf-8"))
    if re.fullmatch(r"[0-9a-f]{40}", tal_commit) is None:
        raise ValueError("PROFILE_TAL_PIN_INVALID")
    packages = []
    seen = set()
    items = report.get("install")
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError("PROFILE_RESOLUTION_INVALID")
    for item in items:
        metadata = item["metadata"]
        name, version = canonical(metadata["name"]), metadata["version"]
        if (name in seen or re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) is None
                or re.fullmatch(r"[A-Za-z0-9.]+", version) is None):
            raise ValueError("PROFILE_PACKAGE_INVALID")
        download = item["download_info"]
        url = urlsplit(download["url"])
        digest = download.get("archive_info", {}).get("hashes", {}).get("sha256", "")
        filename = Path(url.path).name
        if (url.scheme != "https" or url.netloc != "files.pythonhosted.org"
                or url.query or url.fragment or not filename.endswith(".whl")
                or re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", filename) is None
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("PROFILE_WHEEL_OR_HASH_INVALID")
        packages.append({"name": name, "version": version, "sha256": digest, "wheel": filename})
        seen.add(name)
    resolved = {item["name"]: item["version"] for item in packages}
    if any(resolved.get(name) != version for name, version in roots.items()):
        raise ValueError("PROFILE_ROOT_VERSION_CHANGED")
    return {
        "schema_version": "safe_agent.runtime_profile.v1",
        "profile_id": "coordinated-offline-linux-cpython312",
        "target": {"python": "3.12", "implementation": "CPython", "system": "Linux",
                   "machine": "x86_64", "libc": "glibc"},
        "tal": {"repository": TAL_REPOSITORY, "commit": tal_commit},
        "inputs_sha256": hashlib.sha256(raw).hexdigest(),
        "roots": roots, "packages": sorted(packages, key=lambda item: item["name"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolve", action="store_true", help="explicitly access PyPI and create candidate lock")
    parser.add_argument("--check", action="store_true", help="check committed profile/lock agreement without networking")
    args = parser.parse_args()
    if args.resolve == args.check:
        parser.error("choose exactly one of --resolve or --check")
    raw = INPUT.read_bytes()
    pin = (ROOT / "ci/tal-office-commit.sha").read_text(encoding="ascii").strip()
    if args.check:
        value = json.loads(PROFILE.read_text(encoding="utf-8"))
        if (value["inputs_sha256"] != hashlib.sha256(raw).hexdigest()
                or value["roots"] != input_roots(raw.decode("utf-8"))
                or value["tal"] != {"repository": TAL_REPOSITORY, "commit": pin}
                or LOCK.read_text(encoding="ascii") != render_lock(value)):
            raise ValueError("PROFILE_LOCK_DRIFT")
        print("COORDINATED_PROFILE_LOCK_AGREEMENT=PASS")
        return 0
    if (sys.version_info[:2] != (3, 12) or platform.python_implementation() != "CPython"
            or platform.system() != "Linux" or platform.machine() != "x86_64"
            or platform.libc_ver()[0] != "glibc"):
        raise ValueError("PROFILE_RESOLUTION_TARGET_UNSUPPORTED")
    input_roots(raw.decode("utf-8"))
    with tempfile.TemporaryDirectory(prefix="safe_profile_resolve_") as temp:
        report_path = Path(temp) / "resolution.json"
        subprocess.run([
            sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check",
            "install", "--no-input", "--dry-run", "--ignore-installed", "--only-binary=:all:",
            "--index-url", "https://pypi.org/simple", "--report", str(report_path),
            "-r", str(INPUT),
        ], check=True, timeout=180)
        value = profile_from_report(json.loads(report_path.read_text(encoding="utf-8")), raw, pin)
    DEST.mkdir(parents=True, exist_ok=True)
    PROFILE.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    LOCK.write_text(render_lock(value), encoding="ascii")
    print("BEGIN_COORDINATED_PROFILE")
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    print("END_COORDINATED_PROFILE")
    print("BEGIN_COORDINATED_LOCK")
    print(render_lock(value), end="")
    print("END_COORDINATED_LOCK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
