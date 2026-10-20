# Model-only container profile and offline isolation probe

This is an explicit alternative deployment of the existing episode model
sidecar. Its acceptance probe addresses the implementation plan's attempted-read
gate for **this image and launch profile**. Launching the sidecar directly with
Python does not apply these restrictions.

The supported setup is Linux with a local Docker Engine at
`/var/run/docker.sock`, operated from a nonroot account with a nonzero primary
group ID. The container runs with that account's numeric UID/GID so the existing
owner-only socket and key checks remain valid. Remote Docker daemons, Docker
Desktop, rootless Docker UID mapping, and alternate runtimes have not been
validated by this profile.

## Build and run the free probe

From the SAFE repository root:

```bash
SAFE_EPISODE_BUILD_DIR="$(mktemp -d)"
python scripts/episode_service_container.py prepare-context --output "$SAFE_EPISODE_BUILD_DIR"
docker build --pull --tag safe-episode-model:review "$SAFE_EPISODE_BUILD_DIR"
SAFE_EPISODE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' safe-episode-model:review)"
python scripts/episode_service_container.py probe --image "$SAFE_EPISODE_IMAGE_ID" --output episode-container-probe.json
```

The context generator copies only the reviewed Python source allowlist in
`docker/episode_service.sources.json`, the model-service dependency lock, the
Dockerfile, and the probe. The resulting image contains no τ package, benchmark
source/data, SAFE benchmark runner/inventory, domain catalog, tests, repository
checkout, or run artifacts. Existing shared package imports account for some
office contract/storage classes in the allowlist; they do not receive an office
database or execution credentials. The base image is pinned by digest and Python
dependencies are installed with exact versions and distribution hashes.

The probe boots the actual sidecar with synthetic keys and `--network=none`.
It checks health, verifies that an unauthenticated inference request is refused,
and confirms that the durable provider-dispatch table remains empty. It then
executes the attempted-read probe inside that running container. The probe:

- Reads its dedicated provider and dispatch test keys as intended, while
  verifying that those bind mounts are read-only.
- Attempts to read existing host evaluator and harness-secret canary files
  through their absolute paths, `/proc/1/root`, and `/proc/self/root`; all six
  reads must fail.
- Checks that synthetic host credential/data environment variables are absent
  from both the probe process and the actual service process.
- Checks nonroot identity, zero capabilities, `NoNewPrivs`, a read-only root
  filesystem, the exact three mounts, no host PID namespace, no Docker socket,
  and the image's source inventory.

No successful inference is submitted and no model call is made. The probe
cleans up its container and synthetic files. Its report contains fixed aggregate
results and the exact image ID, without canary contents or credentials.
The `container-isolation-probe` job in `tau3.yml` runs this probe as a mandatory
gate, with no fallback or optional skip when Docker is unavailable.

## Use the same profile for an explicitly configured hosted pilot

After the free probe passes, provision an empty, owner-only `0700` service-state
directory outside the repository. Provision separate owner-only `0600` files
for the real agent-provider key and dispatch key outside that state directory.
Use a nonroot account that also runs the benchmark harness. The launcher rejects
symlinks, multiply linked key files, repository paths, unrelated state-directory
contents, mutable image tags, and extra mount/environment/runtime arguments.

```bash
python scripts/episode_service_container.py run \
  --image "$SAFE_EPISODE_IMAGE_ID" \
  --service-dir /absolute/private/model-only-state \
  --dispatch-key /absolute/private/dispatch-key \
  --provider-key /absolute/private/agent-provider-key \
  --model gpt-4.1-2025-04-14 \
  --provider-network
```

The launcher reports the container ID and host socket path. Configure the
benchmark host with `SAFE_EPISODE_SOCKET` pointing to
`/absolute/private/model-only-state/episode.sock` and
`SAFE_EPISODE_DISPATCH_KEY_FILE` pointing to the same host dispatch-key file.
The existing hosted-pilot configuration and budget steps in
[TAU3_RUNTIME.md](TAU3_RUNTIME.md) still apply. The official user simulator and
judge credentials stay in the host harness; they are not container mounts or
forwarded environment variables. Stop the service with `docker stop` and its
reported container ID. Retain the state directory across restarts so its durable
request fence is preserved.

The launcher defaults to network disabled. `--provider-network` deliberately
changes the network to Docker's bridge so the fixed-origin model backend can
reach the provider. This can incur charges when the harness submits inference.
It does **not** add or certify an OS-level destination allowlist; the offline
network-disabled probe is not evidence of hosted egress confinement.

## Scope of the result

The evidence is specific to the tested image, explicit bind mounts, environment,
process settings, and attempted paths. It does not establish generic hostile-code
containment, kernel or Docker-daemon security, protection from a privileged host
administrator, encrypted storage, universal PII removal, or network-egress
certification. The sidecar intentionally has access to its own provider/dispatch
keys, durable fence state, and projected model requests. The caller, Python/SDK
code, container engine, and host configuration remain trusted components.
