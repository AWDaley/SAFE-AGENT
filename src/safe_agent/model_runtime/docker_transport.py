"""Fail-closed, no-network Docker transport for one minimized planner message.

Only a locally installed, exact image SHA may run; the host never passes
credential env vars, host mounts, Docker socket or provider connectivity into
the worker. Requires an independently secured container daemon/runtime host.
Docker itself is privileged host infrastructure, NOT an untrusted model tool.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints

from safe_agent.agent.interfaces import SafeTaskContext
from safe_agent.agent.json_planner import PLANNER_INSTRUCTION
from safe_agent.canonical import canonical_json
from safe_agent.contracts.actions import parse_proposal_json
from safe_agent.contracts.base import StrictModel
from safe_agent.contracts.office import Projection

LocalImageId = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

# This is a conservative BYTE upper bound on standard nonempty tokenizer
# tokens, NOT a measured token count or an actual provider invoice.
_ONE_MILLION = 1_000_000
_MAX_PROTOCOL_BYTES = 16_384


class ModelIsolationDenied(ValueError):
    """Safe public exception: never includes prompt, stdout, container or host secrets."""


class ModelIsolationPolicy(StrictModel):
    """Trusted-host policy: no user/browser/model can modify the launch controls."""

    schema_version: str = Field(default="safe_agent.model_isolation_policy.v1",
                                pattern=r"^safe_agent\.model_isolation_policy\.v1$")
    image_id: LocalImageId
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    max_prompt_bytes: int = Field(default=8192, ge=512, le=_MAX_PROTOCOL_BYTES)
    max_response_bytes: int = Field(default=2048, ge=256, le=_MAX_PROTOCOL_BYTES)
    max_prompt_token_proxy: int = Field(default=8192, ge=512, le=_MAX_PROTOCOL_BYTES)
    max_response_token_proxy: int = Field(default=2048, ge=256, le=_MAX_PROTOCOL_BYTES)
    max_estimated_cost_microusd: int = Field(default=50_000, ge=0, le=10_000_000)
    input_microusd_per_million_tokens: int = Field(default=0, ge=0, le=1_000_000_000)
    output_microusd_per_million_tokens: int = Field(default=0, ge=0, le=1_000_000_000)
    memory_mebibytes: int = Field(default=128, ge=64, le=1024)
    cpu_millicores: int = Field(default=500, ge=100, le=1000)
    pids_limit: int = Field(default=32, ge=8, le=64)


def _ceil_div(x: int, denominator: int) -> int:
    return (x + denominator - 1) // denominator


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ModelIsolationDenied("duplicate planner input key")
        result[key] = value
    return result


def _safe_prompt(prompt: str, *, policy: ModelIsolationPolicy) -> bytes:
    if not isinstance(prompt, str):
        raise ModelIsolationDenied("planner input must be text")
    try:
        data = prompt.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ModelIsolationDenied("planner input encoding denied") from error
    if (len(data) == 0 or len(data) > policy.max_prompt_bytes
            or len(data) > policy.max_prompt_token_proxy):
        raise ModelIsolationDenied("planner input budget exceeded")
    try:
        obj = json.loads(prompt, object_pairs_hook=_reject_duplicates)
        if not isinstance(obj, dict) or set(obj) != {
            "instruction", "task", "availability",
        } or obj["instruction"] != PLANNER_INSTRUCTION:
            raise ModelIsolationDenied("unrecognized planner envelope")
        context = SafeTaskContext.model_validate_json(
            json.dumps(obj["task"], allow_nan=False),
        )
        available = Projection.model_validate_json(
            json.dumps(obj["availability"], allow_nan=False),
        )
    except (ValueError, TypeError, KeyError) as error:
        raise ModelIsolationDenied("invalid scoped planner envelope") from error
    if (context.task_id != available.task_id
            or context.organization_id != available.organization_id
            or context.calendar_ref != available.calendar_ref
            or context.allowed_start >= context.allowed_end
            or available.fetched_at >= available.expires_at
            or context.simulation_ref != "simulation_pending"):
        raise ModelIsolationDenied("planner scope mismatch")
    # This is the only accepted wire format. Do not append tool schemas,
    # protected references, provider IDs, arbitrary user text or workspace logs.
    expected = canonical_json({
        "instruction": PLANNER_INSTRUCTION,
        "task": context,
        "availability": available,
    })
    if expected != data:
        raise ModelIsolationDenied("noncanonical planner input")
    return data


class BoundedDockerRunner:
    """Bounded binary pipes: no unbounded communicate() or persisted prompt."""

    def run(
        self, argv: list[str], prompt: bytes, *,
        timeout_seconds: int, max_output_bytes: int,
    ) -> bytes:
        deadline = time.monotonic() + timeout_seconds
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                     "DOCKER_CONFIG": "/nonexistent"},
                cwd="/", close_fds=True, start_new_session=True, bufsize=0,
            )
        except (OSError, ValueError) as error:
            raise ModelIsolationDenied("isolated runtime unavailable") from error

        output = bytearray()
        pending = memoryview(prompt)
        written = 0
        selector = selectors.DefaultSelector()
        try:
            if proc.stdin is None or proc.stdout is None:
                raise ModelIsolationDenied("isolated runtime pipe unavailable")
            os.set_blocking(proc.stdin.fileno(), False)
            os.set_blocking(proc.stdout.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE)
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelIsolationDenied("isolated runtime time budget exceeded")
                for key, _ in selector.select(timeout=min(remaining, 0.1)):
                    if key.fileobj is proc.stdin:
                        try:
                            written += os.write(
                                proc.stdin.fileno(), pending[written:written + 4096],
                            )
                        except (BrokenPipeError, OSError) as error:
                            raise ModelIsolationDenied(
                                "isolated runtime closed input unexpectedly",
                            ) from error
                        if written >= len(prompt):
                            selector.unregister(proc.stdin)
                            proc.stdin.close()
                    else:
                        try:
                            chunk = os.read(proc.stdout.fileno(), 4096)
                        except OSError as error:
                            raise ModelIsolationDenied("isolated runtime read failed") from error
                        if not chunk:
                            selector.unregister(proc.stdout)
                            proc.stdout.close()
                        else:
                            output.extend(chunk)
                            if len(output) > max_output_bytes:
                                raise ModelIsolationDenied(
                                    "isolated runtime output budget exceeded",
                                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelIsolationDenied("isolated runtime time budget exceeded")
            try:
                exit_code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise ModelIsolationDenied("isolated runtime time budget exceeded") from error
            if exit_code != 0:
                raise ModelIsolationDenied("isolated runtime returned no valid proposal")
            return bytes(output)
        finally:
            selector.close()
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()
            pending.release()


class DockerIsolatedTextGenerator:
    """Drop-in generate(text)->text for ClosedJsonPlanner, not a model tool.

    Must use an image built/audited out of band and selected only by the
    trusted host as a full local content SHA. No image pull, network, mounts,
    credential env forwarding or privileged fallback is allowed.
    """

    def __init__(
        self, policy: ModelIsolationPolicy, *,
        docker_binary: str = "/usr/bin/docker",
    ) -> None:
        if (not docker_binary.startswith("/")
                or "\x00" in docker_binary
                or Path(docker_binary).name != "docker"):
            raise ValueError("docker executable must be a trusted absolute path")
        self._binary = docker_binary
        self._policy = policy
        self._runner = BoundedDockerRunner()

    def _argv(self, cidfile: Path) -> list[str]:
        policy = self._policy
        mib = policy.memory_mebibytes
        return [
            self._binary, "run", "--rm", "--pull=never",
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=65534:65534",
            # Docker's default is an isolated PID namespace; the explicit
            # '--pid=private' value is invalid on supported Docker Engine.
            "--ipc=none", "--cgroupns=private",
            f"--pids-limit={policy.pids_limit}",
            f"--memory={mib}m", f"--memory-swap={mib}m",
            f"--cpus={policy.cpu_millicores / 1000:.3f}",
            "--ulimit=nofile=64:64",
            "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--log-driver=none", "--workdir=/",
            f"--cidfile={cidfile}", "-i", policy.image_id,
        ]

    def _cleanup_failed_container(self, cidfile: Path) -> None:
        try:
            container_id = cidfile.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return
        if re.fullmatch(r"[a-f0-9]{64}", container_id) is None:
            return
        try:
            subprocess.run(
                [self._binary, "rm", "-f", container_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=3, check=False,
                cwd="/", close_fds=True,
                env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                     "DOCKER_CONFIG": "/nonexistent"},
            )
        except (OSError, subprocess.TimeoutExpired):
            # The trusted host must alert on cleanup failure; it is NOT proof
            # that the sandbox stopped. Never try an unconfined fallback.
            pass

    def __call__(self, prompt: str) -> str:
        return self.generate(prompt)

    def generate(self, prompt: str) -> str:
        policy = self._policy
        data = _safe_prompt(prompt, policy=policy)
        max_output = min(policy.max_response_bytes, policy.max_response_token_proxy)
        cost_upper = (
            _ceil_div(
                len(data) * policy.input_microusd_per_million_tokens,
                _ONE_MILLION,
            )
            + _ceil_div(
                max_output * policy.output_microusd_per_million_tokens,
                _ONE_MILLION,
            )
        )
        if cost_upper > policy.max_estimated_cost_microusd:
            raise ModelIsolationDenied("isolated planning cost ceiling exceeded")
        # Never place task/projection data in argv, env, logs or a temporary file.
        with tempfile.TemporaryDirectory(prefix="safe_agent_model_container_") as tmp:
            cidfile = Path(tmp) / "cid"
            try:
                output = self._runner.run(
                    self._argv(cidfile), data,
                    timeout_seconds=policy.timeout_seconds,
                    max_output_bytes=max_output,
                )
            except ModelIsolationDenied as error:
                self._cleanup_failed_container(cidfile)
                # Only messages emitted by the trusted BoundedDockerRunner
                # are safe for diagnostics. A substituted runner must never
                # inject provider stderr, prompt text or secrets into errors.
                fixed_codes = {
                    "isolated runtime unavailable",
                    "isolated runtime pipe unavailable",
                    "isolated runtime time budget exceeded",
                    "isolated runtime closed input unexpectedly",
                    "isolated runtime read failed",
                    "isolated runtime output budget exceeded",
                    "isolated runtime returned no valid proposal",
                }
                if str(error) in fixed_codes:
                    raise
                raise ModelIsolationDenied(
                    "isolated planner execution denied",
                ) from error
            except OSError as error:
                self._cleanup_failed_container(cidfile)
                raise ModelIsolationDenied("isolated planner execution denied") from error
        if not 0 < len(output) <= max_output:
            raise ModelIsolationDenied("isolated runtime output budget exceeded")
        try:
            result = output.decode("utf-8", errors="strict")
            # A model cannot return a completion flag or a generic tool call.
            parse_proposal_json(result)
        except (UnicodeError, ValueError, TypeError) as error:
            raise ModelIsolationDenied("isolated planner response invalid") from error
        return result
