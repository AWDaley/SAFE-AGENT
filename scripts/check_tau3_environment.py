"""Verify the separate offline tau3 CI environment without calling a provider.

The source checkout is evaluator-side material: only counts/hashes are emitted.
The planner never receives task records from this script. Reviewed TAL must be
the actual non-editable installation; a source-path override is not accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from safe_agent.benchmarks.tau3 import EXPECTED_TASK_COUNTS, TAU_COMMIT, TAU_RELEASE
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.inventory import inspect_benchmark
from safe_agent.domains._catalog_data import CATALOG
from safe_agent.tal.domain_runtime import (
    REVIEWED_TAL_BENCHMARK_COMMIT,
    require_reviewed_domain_runtime,
)

ROOT = Path(__file__).resolve().parents[1]
_PROVIDER_CREDENTIALS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "SAFE_AGENT_EPISODE_PROVIDER_API_KEY",
)


def check_environment(tau_root: Path) -> dict[str, Any]:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("The benchmark CI profile requires Python 3.12")
    if any(os.environ.get(name) for name in _PROVIDER_CREDENTIALS):
        raise RuntimeError("Offline benchmark CI must not receive provider credentials")
    if os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") != "True":
        raise RuntimeError("Set LITELLM_LOCAL_MODEL_COST_MAP=True for offline import behavior")
    pin = (ROOT / "ci/tal-benchmark-commit.sha").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", pin) or pin != REVIEWED_TAL_BENCHMARK_COMMIT:
        raise RuntimeError("Benchmark TAL pin and reviewed runtime disagree")
    require_reviewed_domain_runtime()

    root = tau_root.resolve(strict=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    if commit != TAU_COMMIT:
        raise RuntimeError("The tau checkout is not the frozen benchmark commit")
    spec = importlib.util.find_spec("tau2")
    if (
        spec is None
        or spec.origin is None
        or Path(spec.origin).resolve() != root / "src/tau2/__init__.py"
    ):
        raise RuntimeError("PYTHONPATH must resolve tau2 to the content-verified checkout")
    if Path(os.environ.get("TAU2_DATA_DIR", "")).resolve() != root / "data":
        raise RuntimeError("TAU2_DATA_DIR must bind the verified checkout's own data")
    if importlib.metadata.version("tau2") != TAU_RELEASE.removeprefix("v"):
        raise RuntimeError("Installed tau2 metadata differs from the frozen release")
    installed = importlib.metadata.distribution("tau2")
    direct = json.loads(installed.read_text("direct_url.json") or "null")
    if (
        not isinstance(direct, dict)
        or direct.get("url") != "https://github.com/sierra-research/tau2-bench.git"
        or not isinstance(direct.get("vcs_info"), dict)
        or direct["vcs_info"].get("vcs") != "git"
        or direct["vcs_info"].get("commit_id") != TAU_COMMIT
    ):
        raise RuntimeError("Installed tau2 must come from the exact pinned VCS dependency")

    inventories = {}
    for domain, expected in EXPECTED_TASK_COUNTS.items():
        config = BenchmarkConfig.model_validate({"domain": domain, "stage": "coverage"})
        result = inspect_benchmark(config, root)
        if (
            result.get("status") != "ready"
            or not result.get("source_verified")
            or result.get("domain_task_count") != expected
            or result.get("selected_task_count") != expected
        ):
            raise RuntimeError(f"Frozen {domain} source inventory verification failed")
        inventories[domain] = {
            "tasks": result["domain_task_count"],
            "source_files": result["source_verification"]["verified_files"],
            "manifest_sha256": result["source_verification"]["manifest_sha256"],
            "task_content_sha256": result["task_content_sha256"],
        }
    catalog: Any = CATALOG
    if catalog["upstream_commit"] != TAU_COMMIT:
        raise RuntimeError("Capability catalog revision differs from benchmark revision")
    for relative, expected_hash in catalog["public_source_sha256"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected_hash:
            raise RuntimeError("Capability catalog public-source provenance failed")
    return {
        "status": "verified",
        "python": ".".join(map(str, sys.version_info[:3])),
        "tau_commit": TAU_COMMIT,
        "tal_commit": pin,
        "inventories": inventories,
        "provider_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tau-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check_environment(args.tau_root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
