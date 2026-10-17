"""No-network dependency preflight. A passing report is never a dispatch permit.

No installation, socket probe, database open, token read or subprocess occurs.
Wheel hashes are enforced by installation, not re-attested by version metadata.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import metadata

from safe_agent.runtime.profile import PROFILE_ID, TARGET, RuntimeProfile, canonical, load_profile

SOURCE_VERSIONS = {"tervoro-safe-agent": "0.1.0", "tervoro-agentic-language": "0.4.0rc2"}
SELECTED_EXTRAS = {"tervoro-safe-agent": ("dev", "api", "postgres", "coordinated-offline"),
                   "psycopg": ("binary",)}


@dataclass(frozen=True, order=True)
class Diagnostic:
    code: str
    package: str = ""


@dataclass(frozen=True)
class InstalledPackage:
    name: str
    version: str
    requires: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreflightReport:
    status: str
    profile_id: str
    profile_sha256: str | None
    locked_packages: int
    diagnostics: tuple[Diagnostic, ...]
    schema_version: str = "safe_agent.runtime_preflight.v1"
    scope: str = "DEPENDENCY_ENVIRONMENT_ONLY"
    production_authorized: bool = False
    live_google_write_verified: bool = False
    hosted_inference_enabled: bool = False
    business_execution_authorized: bool = False


def current_target() -> dict[str, str]:
    return {"python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "implementation": platform.python_implementation(), "system": platform.system(),
            "machine": platform.machine(), "libc": platform.libc_ver()[0]}


def collect_inventory() -> tuple[InstalledPackage, ...]:
    result: list[InstalledPackage] = []
    for installed in metadata.distributions():
        if len(result) >= 500:
            raise ValueError("too many distributions")
        name = installed.metadata.get("Name")
        if not name:
            raise ValueError("unnamed distribution")
        requirements = tuple(installed.requires or ())
        if len(requirements) > 2000 or any(len(value) > 4096 for value in requirements):
            raise ValueError("unbounded dependency metadata")
        result.append(InstalledPackage(canonical(name), installed.version, requirements,
                                       tuple(installed.metadata.get_all("Provides-Extra") or ())))
    return tuple(result)


def check_metadata(profile: RuntimeProfile, inventory: tuple[InstalledPackage, ...]) -> set[Diagnostic]:
    expected = {pin.name: pin.version for pin in profile.packages} | SOURCE_VERSIONS
    problems: set[Diagnostic] = set()
    installed: dict[str, InstalledPackage] = {}
    for package in inventory:
        name = canonical(package.name)
        if name not in expected:
            problems.add(Diagnostic("UNLOCKED_DISTRIBUTION"))
            continue
        if name in installed:
            problems.add(Diagnostic("DUPLICATE_DISTRIBUTION", name))
        installed[name] = package
    for name, version in expected.items():
        found = installed.get(name)
        if found is None:
            problems.add(Diagnostic("PACKAGE_MISSING", name))
        elif found.version != version:
            problems.add(Diagnostic("PACKAGE_VERSION_MISMATCH", name))
    if problems:
        return problems
    # Imports only the locked metadata parser after versions have been checked.
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version

        pending = [(name, "") for name in expected]
        pending.extend((name, extra) for name, extras in SELECTED_EXTRAS.items() for extra in extras)
        examined: set[tuple[str, str]] = set()
        while pending:
            name, extra = pending.pop()
            if (name, extra) in examined:
                continue
            examined.add((name, extra))
            package = installed[name]
            if extra and extra not in {canonical(value) for value in package.extras}:
                problems.add(Diagnostic("REQUIRED_EXTRA_MISSING", name))
            for raw in package.requires:
                requirement = Requirement(raw)
                if requirement.marker is not None and not requirement.marker.evaluate({"extra": extra}):
                    continue
                dependency = canonical(requirement.name)
                if requirement.url is not None or dependency not in expected:
                    problems.add(Diagnostic("DEPENDENCY_NOT_LOCKED", name))
                    continue
                if Version(installed[dependency].version) not in requirement.specifier:
                    problems.add(Diagnostic("DEPENDENCY_CONFLICT", name))
                pending.append((dependency, ""))
                pending.extend((dependency, canonical(value)) for value in requirement.extras)
    except Exception:
        problems.add(Diagnostic("DEPENDENCY_METADATA_INVALID"))
    return problems


def _verify_tal(profile: RuntimeProfile) -> None:
    from safe_agent.tal.reviewed_runtime import (
        REVIEWED_TAL_COMMIT,
        REVIEWED_TAL_REPOSITORY,
        require_reviewed_tal_runtime,
    )

    if (profile.tal_repository != REVIEWED_TAL_REPOSITORY
            or profile.tal_commit != REVIEWED_TAL_COMMIT):
        raise ValueError("profile disagrees with dispatch runtime")
    require_reviewed_tal_runtime()


def inspect_environment() -> PreflightReport:
    problems: set[Diagnostic] = set()
    profile = None
    try:
        profile = load_profile()
    except Exception:
        problems.add(Diagnostic("RUNTIME_PROFILE_INVALID"))
    try:
        if current_target() != TARGET:
            problems.add(Diagnostic("PLATFORM_UNSUPPORTED"))
        if sys.prefix == sys.base_prefix:
            problems.add(Diagnostic("ISOLATED_ENVIRONMENT_REQUIRED"))
    except Exception:
        problems.add(Diagnostic("PLATFORM_UNAVAILABLE"))
    if profile is not None:
        try:
            problems.update(check_metadata(profile, collect_inventory()))
        except Exception:
            problems.add(Diagnostic("ENVIRONMENT_METADATA_INVALID"))
        # Do not import TAL when its dependency environment is already invalid.
        if not problems:
            try:
                _verify_tal(profile)
            except Exception:
                problems.add(Diagnostic("TAL_PROVENANCE_UNVERIFIED"))
    return PreflightReport(
        "BLOCKED" if problems else "PASS", PROFILE_ID,
        profile.profile_sha256 if profile else None,
        len(profile.packages) if profile else 0, tuple(sorted(problems)),
    )


def main(argv: list[str] | None = None, *,
         inspect: Callable[[], PreflightReport] = inspect_environment) -> int:
    parser = argparse.ArgumentParser(description="Inspect offline dependencies; never start an agent.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics (default)")
    parser.parse_args(argv)
    try:
        report = inspect()
    except Exception:
        report = PreflightReport("BLOCKED", PROFILE_ID, None, 0, (Diagnostic("PREFLIGHT_UNAVAILABLE"),))
    print(json.dumps(asdict(report), sort_keys=True))
    return 0 if report.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
