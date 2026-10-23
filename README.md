# TERVORO SAFE-AGENT

SAFE-AGENT is an experimental, contract-first office agent with a trusted
execution boundary. Models propose actions; scoped references, TAL policy,
exact-action human approval, durable dispatch and independent readback govern
whether an action may execute and be reported complete.

## Current status

**M5 LIVE ACCEPTANCE: BLOCKED.**

The supported application is a **persistent offline local office using synthetic
business records and an offline inference backend**. Durable inference admission,
dispatch, result storage, coordinated planning, the operator recovery console,
pre-effect task retirement, accounting reconciliation and workspace maintenance
are implemented. The older milestone documents describe the state at their own
checkpoints; they are not the current setup instructions.

| Capability | Current boundary | Details |
| --- | --- | --- |
| Persistent office | Local API/UI, separate inference sidecar and explicit worker; synthetic records | [Local office runbook](docs/PERSISTENT_LOCAL_OFFICE.md) |
| Inference | Durable reservations, fenced dispatch and validated stored results | [Coordinated dispatch](docs/M7_COORDINATED_DISPATCH.md), [durable results](docs/W3_DURABLE_INFERENCE_RESULTS.md) |
| Planning and actions | Current source binding, pinned TAL checks, exact proposal approval and independent synthetic readback | [Coordinated planning](docs/W4_COORDINATED_PLANNING.md), [dispatch and revocation](docs/REFERENCE_REVOCATION_AND_DISPATCH.md) |
| Recovery | Conservative handling of uncertainty, eligible task retirement and separate accounting evidence | [Operator recovery](docs/W4_OPERATOR_RECOVERY.md), [retirement](docs/TASK_RETIREMENT.md), [accounting](docs/INFERENCE_ACCOUNTING_RECONCILIATION.md) |
| Maintenance | Explicit stopped-process upgrade/rollback and signed billing-keyring distribution | [Workspace maintenance](docs/WORKSPACE_MAINTENANCE.md), [verifier identity](docs/ASYMMETRIC_BILLING_VERIFIER.md) |
| Google Calendar | OAuth/connector and recovery fixtures are tested offline; live acceptance remains blocked | [Google boundary](docs/M5_DELIVERY.md), [release gate](docs/M5_RELEASE_GATE.md) |

This repository does not claim production approval or verified live Google
execution. The local development topology shares one workstation identity;
logical component boundaries are not an operating-system sandbox. Benchmark
experiments and provider-specific billing adapters have their own configuration,
acceptance and review requirements; they do not activate the office application.

## Supported installation

Use **CPython 3.12 on Linux x86_64 with glibc**, from this repository's root.
The coordinated profile pins the complete third-party dependency closure and
installs the required TAL source revision from
[`ci/tal-office-commit.sha`](ci/tal-office-commit.sha).

```sh
python3.12 scripts/install_coordinated_runtime.py
.venv-coordinated/bin/safe-agent-doctor --json
```

The installer creates a new `.venv-coordinated` and downloads the reviewed
packages/source. It refuses an existing destination; use `--venv /new/path` for
a separate installation. A successful doctor report verifies the dependency
profile only. See the [runtime profile](docs/COORDINATED_RUNTIME_PROFILE.md) for
supported targets, provenance and diagnostics.

`requirements.lock` is a historical fixture environment, not the supported
combined SAFE/TAL installation. Do not combine the legacy `google-test` extra
with `coordinated-offline`; their cryptography requirements conflict.

## Start the local office

Choose a new, short private workspace path. Initialization creates the synthetic
source and database once; normal startup never reseeds or migrates it.

```sh
.venv-coordinated/bin/safe-agent-local --workspace /tmp/safe-office \
  init --migrations "$PWD/migrations"
.venv-coordinated/bin/safe-agent-local --workspace /tmp/safe-office status
```

Then follow the [local office runbook](docs/PERSISTENT_LOCAL_OFFICE.md) to start
the sidecar and API in separate terminals, obtain requester/approver sessions,
review and approve a proposal, and run the worker. The API binds to loopback;
planning or approval alone does not start the action worker.

For shorter demonstrations using only packaged synthetic records:

```sh
.venv-coordinated/bin/python -m safe_agent virtual-office-demo
.venv-coordinated/bin/python -m safe_agent m4-demo
```

See [packaged synthetic demos](docs/PACKAGED_SYNTHETIC_DEMOS.md) for their scopes.

## Development verification

The installer builds the selected checkout. To test subsequent source edits,
select the checkout explicitly; installed-process acceptance requires rebuilding
and installing the updated package in a new reviewed environment.

```sh
PYTHONPATH=src .venv-coordinated/bin/python -m pytest
.venv-coordinated/bin/ruff check src tests scripts
.venv-coordinated/bin/mypy src/safe_agent
PYTHONPATH=src .venv-coordinated/bin/python scripts/export_schemas.py --check
```

The workflows also check the milestone schema exports, real PostgreSQL
constraints/concurrency, process restarts, browser flows and container isolation.
A local run may skip environment-gated acceptance tests; only executed checks
are evidence of passing. Tests and fixtures do not supply live-provider approval.

## Design and history

- [System design](docs/SAFE_AGENT_DESIGN.md) and [implementation plan](docs/MVP_IMPLEMENTATION_PLAN.md).
- [Security conformance matrix](docs/security-test-matrix.md) and [integration review](docs/M7_INTEGRATION_SECURITY_REVIEW.md).
- [M1](docs/M1_DELIVERY.md), [M2](docs/M2_DELIVERY.md), [M3](docs/M3_DELIVERY.md), [M4](docs/M4_DELIVERY.md) and [M5](docs/M5_DELIVERY.md) historical delivery records.
- [Operator intake](docs/M6_TASK_INTAKE.md), [synthetic workflow](docs/M6_SYNTHETIC_WORKFLOW.md) and [inference contracts](docs/M7_INFERENCE_CONTRACTS.md).
- [Upstream TAL](https://github.com/AWDaley/TervoroAgenticLanguage-TAL-) and [compatibility findings](docs/tal-compatibility.md). A TAL execution record is trusted evidence, not a bearer capability.
