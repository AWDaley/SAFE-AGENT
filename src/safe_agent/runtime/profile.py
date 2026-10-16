"""Closed, packaged dependency profile. Parsing does not import third-party code."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_ID = "coordinated-offline-linux-cpython312"
TARGET = {"python": "3.12", "implementation": "CPython", "system": "Linux",
          "machine": "x86_64", "libc": "glibc"}
STEM = "coordinated-linux-py312"


class ProfileInvalid(ValueError):
    def __init__(self) -> None:
        super().__init__("RUNTIME_PROFILE_INVALID")


@dataclass(frozen=True)
class PackagePin:
    name: str
    version: str
    sha256: str
    wheel: str


@dataclass(frozen=True)
class RuntimeProfile:
    packages: tuple[PackagePin, ...]
    roots: tuple[tuple[str, str], ...]
    tal_repository: str
    tal_commit: str
    inputs_sha256: str
    profile_sha256: str


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProfileInvalid()
        value[key] = item
    return value


def parse_profile(raw: bytes, lock: bytes) -> RuntimeProfile:
    try:
        if not 1 <= len(raw) <= 131072 or not 1 <= len(lock) <= 65536:
            raise ProfileInvalid()
        data = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(data, dict) or set(data) != {
                "schema_version", "profile_id", "target", "tal", "inputs_sha256", "roots", "packages"}
                or data["schema_version"] != "safe_agent.runtime_profile.v1"
                or data["profile_id"] != PROFILE_ID or data["target"] != TARGET):
            raise ProfileInvalid()
        tal = data["tal"]
        if (not isinstance(tal, dict) or set(tal) != {"repository", "commit"}
                or tal["repository"] != "https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git"
                or not isinstance(tal["commit"], str)
                or re.fullmatch(r"[0-9a-f]{40}", tal["commit"]) is None
                or not isinstance(data["inputs_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", data["inputs_sha256"]) is None):
            raise ProfileInvalid()
        items = data["packages"]
        if not isinstance(items, list) or not 1 <= len(items) <= 200:
            raise ProfileInvalid()
        pins = []
        seen: dict[str, tuple[str, str]] = {}
        for item in items:
            if not isinstance(item, dict) or set(item) != {"name", "version", "sha256", "wheel"}:
                raise ProfileInvalid()
            if not all(isinstance(value, str) and len(value) <= 240 for value in item.values()):
                raise ProfileInvalid()
            pin = PackagePin(**item)
            if (re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", pin.name) is None
                    or canonical(pin.name) != pin.name or pin.name in seen
                    or re.fullmatch(r"[A-Za-z0-9.]{1,64}", pin.version) is None
                    or re.fullmatch(r"[0-9a-f]{64}", pin.sha256) is None
                    or re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", pin.wheel) is None):
                raise ProfileInvalid()
            seen[pin.name] = (pin.version, pin.sha256)
            pins.append(pin)
        if [pin.name for pin in pins] != sorted(seen):
            raise ProfileInvalid()
        required = {"pip", "setuptools", "wheel", "packaging", "pydantic", "sqlalchemy",
                    "alembic", "cryptography", "httpx", "fastapi", "uvicorn", "psycopg", "psycopg-binary"}
        roots = data["roots"]
        if (not required <= set(seen) or not isinstance(roots, dict) or not roots
                or any(name not in seen or seen[name][0] != version for name, version in roots.items())):
            raise ProfileInvalid()
        locked: dict[str, tuple[str, str]] = {}
        for line in lock.decode("ascii").splitlines():
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([a-z0-9][a-z0-9-]*)==([A-Za-z0-9.]+) --hash=sha256:([0-9a-f]{64})", line)
            if match is None or match[1] in locked:
                raise ProfileInvalid()
            locked[match[1]] = (match[2], match[3])
        if locked != seen:
            raise ProfileInvalid()
        return RuntimeProfile(tuple(pins), tuple(sorted(roots.items())), tal["repository"], tal["commit"],
                              data["inputs_sha256"], hashlib.sha256(raw).hexdigest())
    except ProfileInvalid:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ProfileInvalid() from None


def load_profile() -> RuntimeProfile:
    directory = Path(__file__).resolve().parent
    try:
        return parse_profile((directory / (STEM + ".json")).read_bytes(),
                             (directory / (STEM + ".lock")).read_bytes())
    except OSError:
        raise ProfileInvalid() from None
