# M6-01f — Docker-isolated offline text model runtime

**Status:** Opt-in isolated model boundary, stacked on PR #23 (which depends on
PRs #22, #21, and #20). Independent security review is still required.
**M5 LIVE ACCEPTANCE: BLOCKED. No Google provider writes enabled.**

## Trust boundary

The previous M6-01e ClosedJsonPlanner accepts a trusted Python callback.
That is useful for synthetic tests, but it is NOT an isolation boundary and
must not be wired to an untrusted model process in a privileged office host.

This milestone provides DockerIsolatedTextGenerator as the generate callback
for the existing ClosedJsonPlanner. A trusted helper is available as
TrustedPersistentPlanning.from_isolated_image(...). Only a fully qualified
local image ID (sha256: followed by 64 lowercase hex characters) is accepted.
The runtime refuses mutable tags and registry pulls. Docker is executed
only in a trusted host; it is NEVER available as an LLM tool, FastAPI route,
browser action, model-defined command, or office provider mechanism.

The worker sees the exact existing minimized M0 planner envelope: a fixed
instruction, typed SafeTaskContext and sanitized free/busy Projection. The
host validates exact closed JSON keys, tenant/task/calendar bindings and
canonical bytes before sending STDIN to the worker. Private customer/calendar
resource IDs, connection vaults, database credentials, bearer tokens and
provider API tools are NOT sent. Output must be UTF-8 and exactly one
closed SAFE-AGENT v1 proposal or clarification object.

The model's output has NO authority. Every proposal still needs the trusted
Virtual Office simulation, immutable manifest, TAL-style local policy,
approval separation and independently verified synthetic outbox worker.
No direct model result can dispatch effects, rewrite a manifest, connect
an account or declare task completion.

## Docker confinement

Every launch includes Docker flags that cannot be supplied or altered by a
browser or model:

- --pull=never and --network=none: pinned local image only, no network.
- --read-only plus a 16 MiB noexec/nosuid/nodev temporary filesystem:
  no host mounts or writable root filesystem.
- --cap-drop=ALL, --security-opt=no-new-privileges:true, non-root
  user 65534:65534, and private PID/IPC/cgroup namespaces.
- CPU, memory, memory-swap, process/PID and open-file limits.
- --log-driver=none to avoid persistent model prompt container logs.
- One fresh container per attempt with a host-only CID file for forced
  cleanup when Docker run fails or exceeds its deadline.
- Strict host wall-clock deadline, bounded nonblocking STDIN/STDOUT pipe
  streams and a process group kill for blocked or flooding Docker clients.
- An explicit minimal subprocess environment: no inherited database
  credentials, OAuth secrets or model-provider API tokens.

The Docker socket is NEVER mounted inside the worker. An unavailable
daemon/image, invalid result, timeout, flood or exit failure causes a
safe planning failure without any Python subprocess inference fallback.
Raw worker stderr is discarded, and stdout is never logged to the operator.

**Limitations:** A Docker daemon is powerful host infrastructure. Use a
dedicated, hardened service host or rootless engine rather than exposing
the Docker socket to user-facing agents. A pinned image ID does not prove
signed image provenance or preclude container escapes. Review and scan
the image, pin its base by digest, enable seccomp/LSM protections, and
isolate the Docker host before using an untrusted real model.

## Resource, token and cost controls

The host enforces strict prompt/response UTF-8 byte ceilings and independent,
conservative token proxy ceilings. The token proxy assumes at most one
standard nonempty byte-backed tokenizer token per UTF-8 byte. This is a
safe capacity bound for ordinary text, NOT an exact provider tokenizer
measurement or a verified billing statement. The host calculates a
worst-case per-attempt price estimate from trusted per-million-token rates
and denies any request exceeding the configured micro-USD ceiling BEFORE
the Docker process is created.

The network-disabled offline model fixture cannot call hosted inference
providers and thus cannot generate external API charges. Future remote
model access would require a reviewed egress proxy, isolated inference-only
credentials, actual token/usage validation and persistent spending controls.
The parent planning workspace caps the number of attempts.

## Offline executable proof

The image contains offline_worker.py, a standalone standard-library-only
deterministic text-protocol fixture. It chooses a fitting synthetic time
or an explicit clarification code; it is NOT a deployed LLM.

Build the local image with a reviewed base. The Dockerfile's default
mutable python:3.12-slim base is for CI/demo ONLY; audited deployments
must supply a pinned base image digest:

    docker build --pull -f docker/model_runtime.Dockerfile \
      -t safe-agent-offline-model:demo .
    docker image inspect --format '{{.Id}}' safe-agent-offline-model:demo

Then use its full local image ID:

    export SAFE_AGENT_ISOLATION_IMAGE_ID="$(docker image inspect \
      --format '{{.Id}}' safe-agent-offline-model:demo)"
    python -m safe_agent m6-isolated-model-demo

The CLI creates disposable SQLite task data and submits one synthetic
appointment through the no-network Docker worker. The trusted gate records
one pending human-review proposal. No provider event or credential exists
in the container.

Trusted startup wiring:

    from safe_agent.model_runtime import ModelIsolationPolicy
    from safe_agent.orchestration.persistent_planning import TrustedPersistentPlanning

    image_policy = ModelIsolationPolicy(
        image_id=trusted_local_image_sha,
        timeout_seconds=10,
        max_prompt_token_proxy=8192,
        max_response_token_proxy=2048,
        max_estimated_cost_microusd=0,
    )
    planning = TrustedPersistentPlanning.from_isolated_image(
        store, synthetic_workflow, image_policy=image_policy,
    )

The trusted host supplies the image, policy and Docker binary path. They
cannot be selected by a task, model, or HTTP request. Existing authenticated
planning routes do not gain a provider-execution capability.

## Validation and remaining blockers

Run pytest -q tests/m6/test_isolated_model_runtime.py and the full test
suite. Host-only unit tests verify the pinned image policy, Docker command
fences, rejection of malformed/noncanonical prompts, budget refusal before
launch, closed outputs, bounded subprocess I/O, timeout, explicit no-secret
environment, cleanup and trusted planning integration.

Direct Python execution of the offline protocol fixture is only a unit test,
NOT evidence of OS isolation. The separate GitHub CI Docker step builds the
image, pins its image ID, and invokes the full no-network Docker path.

**Outstanding blockers:** production-ready dedicated Docker runtime host,
rootless identity, image signing/vulnerability scanning, seccomp/LSM review,
audited orphan-container cleanup, model-specific exact token accounting,
hosted inference spending verification, restricted remote egress architecture,
operator SSO, durable incident alerting, and independent live Google/TAL
acceptance. CI passing does not confer a live provider authorization.
