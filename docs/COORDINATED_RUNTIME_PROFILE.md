# Coordinated offline runtime profile and preflight

This profile is the supported dependency environment for the current offline
office application. It provides installation and diagnostics; the
[persistent local launcher](PERSISTENT_LOCAL_OFFICE.md) supplies application
startup. Neither the profile nor the launcher establishes production approval or
an operating-system sandbox.

## Why a separate profile

The reviewed TAL commit requires `cryptography<46`. The original SAFE fixture
`requirements.lock` selects `46.0.4`. Previously a later TAL installation could
silently replace it with `45.0.7`. The new **coordinated-offline** profile explicitly
uses `45.0.7`, the compatibility version already exercised by TAL-enabled CI.
This is not a claim that this older version has been security-certified for
production. Dependency vulnerability review and a future reviewed TAL update
remain separate release work.

The legacy `google-test` extra retains its `46.0.4` requirement. Do not combine it
with `coordinated-offline`; they are intentionally incompatible profiles. No
upstream metadata is rewritten to pretend the conflict does not exist. The
historical fixture lock is retained for legacy tests, not the recommended input
for the combined profile.

## Supported installation target

The wheel lock targets **CPython 3.12, Linux x86_64 with glibc**; acceptance runs on
Ubuntu 24.04. There is no native Windows, macOS, ARM or musl compatibility claim.
This is also consistent with the existing private Unix-socket inference path.
Use an isolated development environment. No customer credentials are needed.

From the reviewed application checkout, with Python 3.12 available:

```sh
python3.12 scripts/install_coordinated_runtime.py
.venv-coordinated/bin/safe-agent-doctor --json
```

The installer creates `.venv-coordinated` only if it does not already exist. A
custom NEW path may be passed with `--venv`. Existing directories and symlinks
are refused; failed partial environments are never automatically deleted or
silently repaired. Inspect a failure before choosing a fresh path.

Installation is an explicit network-using maintenance operation. It installs
third-party wheels with `--require-hashes --only-binary=:all:` from the committed
lock, then installs the pinned TAL source and this SAFE checkout with
`--no-deps --no-build-isolation`. The build tools are included in the wheel lock.
It finishes with `pip check` and the installed preflight outside the checkout.
There is no post-failure unpinned install, dependency override, model route,
database migration or automatic downgrade fallback.

## What is pinned, and what is not

`requirements/coordinated-linux-py312.in` holds exact direct inputs. The packaged
`src/safe_agent/runtime/coordinated-linux-py312.json` records the resolved
third-party dependency closure, artifact hashes, target and TAL commit. Its
adjacent `.lock` is the corresponding pip hash-checking input. Both ship in the
installed wheel so diagnostics do not depend on the current working directory.

The initial candidates came from resolver workflow `38925320963`, job
`117055250209`, on commit `994ec5634d0f652df419480b040dc5f315435acb`.
They were obtained through an actual PyPI dry-run report, not hand-invented
artifact digests. Clean hash-checked installation and final tests are separate
acceptance; exact final evidence belongs in PR #37.

The TAL source is pinned by its existing VCS commit, separately from third-party
wheel hashing. SAFE is built from the selected checkout. This does **not** make
SAFE/TAL wheel builds byte-for-byte reproducible, authenticate a review, pin the
operating-system image, or re-attest installed file bytes. Python/ensurepip is
the installation bootstrap trust root. Full source/build provenance, audit,
OS/base-image pinning and update policy remain open W0 work.

For an explicit maintenance update on the target environment:

```sh
python3.12 scripts/resolve_coordinated_profile.py --resolve
python3.12 scripts/resolve_coordinated_profile.py --check
```

`--resolve` accesses PyPI and writes review candidates; it must not run during
normal startup or recovery. Review the input, lock and profile diff together.
CI acceptance installs the committed lock; it does not regenerate it to make
an unexpected version or hash pass.

## Preflight semantics

`safe-agent-doctor --json` and `python -m safe_agent.runtime --json` have the same
behavior. Exit **0** means the dependency profile matches; exit **2** means
blocked. Diagnostics use fixed codes and allowlisted package names, not raw
exceptions, credentials, resource mappings or arbitrary installed-package names.

Checks cover the target and virtual environment, exact package versions,
duplicate/unlocked distributions, selected extras, transitive dependency
constraints, profile/lock agreement, and the existing reviewed-TAL provenance
check. An incompatible environment is rejected rather than modified.

Preflight never installs packages, starts an inference service, opens a database,
creates a socket connection, reads application secrets, approves an action,
queues an effect, releases a cost hold or renews a lease. No CLI option can grant
production or provider authorization. A successful report explicitly retains:

```json
{"scope":"DEPENDENCY_ENVIRONMENT_ONLY","production_authorized":false,
 "live_google_write_verified":false,"hosted_inference_enabled":false,
 "business_execution_authorized":false}
```

This command is an operator diagnostic, not a replacement for worker enforcement.
The R1 source-binding checks, mandatory worker TAL check and exact-action human
approval remain unchanged. An operator cannot submit this report as a permit.

## Verification boundary

The new dedicated workflow requires a clean hash-checked installation, installed
CLI execution from `/tmp`, no-network/no-subprocess preflight, at least 51
non-skipped unit regressions, a full test invocation under the combined profile,
and 19 non-skipped real PostgreSQL/TAL review-boundary cases. Existing main,
W3/W4, browser, PostgreSQL, restart, Docker isolation and journal checks remain.
Earlier legacy workflows may still use historical fixture installation inputs;
only the dedicated runtime job certifies this new combined lock. Migrating all
legacy CI profiles is not silently claimed complete.

The profile itself grants no business authority. Application startup,
[task retirement](TASK_RETIREMENT.md) and
[workspace maintenance](WORKSPACE_MAINTENANCE.md) have separate controls and
runbooks. **Live Google acceptance remains BLOCKED and production remains
unauthorized.**
