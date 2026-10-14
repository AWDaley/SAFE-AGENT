"""Offline Docker-isolated model protocol demo, with no live model provider.

Docker must be installed and the operator must build/audit the local image,
provide its exact sha256 image ID and isolate the daemon from sensitive hosts.
No uncontained fallback exists.
"""

from __future__ import annotations

import json
import os

from safe_agent.demo.m6_planning import run_m6_planning_demo
from safe_agent.model_runtime import DockerIsolatedTextGenerator, ModelIsolationPolicy


def run_m6_isolated_model_demo() -> dict[str, object]:
    image_id = os.environ.get("SAFE_AGENT_ISOLATION_IMAGE_ID")
    if not image_id:
        raise ValueError("trusted local Docker image ID is required; no fallback")
    policy = ModelIsolationPolicy(
        image_id=image_id,
        timeout_seconds=15,
        max_prompt_bytes=8192,
        max_response_bytes=2048,
        max_prompt_token_proxy=8192,
        max_response_token_proxy=2048,
        max_estimated_cost_microusd=0,
    )
    transport = DockerIsolatedTextGenerator(policy)
    result = run_m6_planning_demo(isolated_generate=transport.generate)
    if (result["workspace_status"] != "PREPARED"
            or result["external_effects"] != 0
            or result["model_stub_calls"] != 1):
        raise AssertionError("isolated offline planning demo did not pass")
    return {
        **result,
        "mode": "M6_ISOLATED_MODEL_DOCKER_NO_NETWORK",
        "sandbox": "docker_no_network_read_only_unprivileged",
        "host_secret_mounts": False,
        "live_model_inference_connected": False,
    }


def print_m6_isolated_model_demo() -> None:
    print(json.dumps(run_m6_isolated_model_demo(), indent=2))
