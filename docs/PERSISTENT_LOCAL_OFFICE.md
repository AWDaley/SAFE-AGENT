# Persistent offline local office

The local development launcher is included in the current application. It is not
production deployment or permission to connect a live provider. It uses the real
coordinated planner, durable result/consumption stores, policy gates, approval API,
TAL-enforcing worker and private Unix-socket inference service. The inference
backend remains the exact offline mock and all business records are synthetic.

## Setup

Use the current reviewed application checkout in the supported
CPython 3.12 / Linux x86_64 / glibc environment. Native Windows is not supported.
Install the committed runtime profile into a NEW environment. The installer uses
the network for dependency/source downloads; starting and using the local office
does not call hosted inference or Google.

```sh
python3.12 scripts/install_coordinated_runtime.py
REPO="$PWD"
BIN="$REPO/.venv-coordinated/bin/safe-agent-local"
WORKSPACE=/tmp/safe-office

# Explicit initialization only. Refuses an existing directory, even an empty one.
"$BIN" --workspace "$WORKSPACE" init --migrations "$REPO/migrations"
"$BIN" --workspace "$WORKSPACE" status
```

The trusted source checkout is required for the existing Alembic migration files
during `init` only. They are not copied into a second migration lineage. No
`create_all`, tests-package import or test-fixture constructor initializes the
application database. Later startup requires the existing current migration head
and provider snapshot; it will not recreate a missing DB or reseed lost state.
The original packaged synthetic source is used only to initialize a NEW workspace.

Choose a short, private absolute workspace path: the complete Unix socket path
must fit within 103 bytes. The parent must exist. Symlinked path components and
existing initialization targets are rejected. A failed partial initialization is
left intact without a READY marker; use a separately reviewed fresh directory,
not an automatic delete/repair option.

## Separate processes

In separate local terminals, using the same BIN and WORKSPACE values:

```sh
# Inference-only service. No TCP listener or hosted model.
"$BIN" --workspace "$WORKSPACE" sidecar

# Operator UI/API. Always binds 127.0.0.1; default port 8765.
"$BIN" --workspace "$WORKSPACE" serve
```

Open `http://127.0.0.1:8765/operator`. The existing recovery console is at
`/operator/recovery`. Liveness is `/healthz`; `/readyz` checks the persistent DB
and offline sidecar. A healthy API without its sidecar is not planning-ready.
Readiness is not evidence that a worker is running or permission for an effect.
The older library `/health` response is retained for compatibility; use the new
launcher endpoints for launcher status.

Sessions are issued only by an explicit local command, never an HTTP endpoint:

```sh
"$BIN" --workspace "$WORKSPACE" session --role requester
"$BIN" --workspace "$WORKSPACE" session --role approver
```

Paste the appropriate token into the existing local operator interface. Tokens
expire after 15 minutes by default; `--seconds` is bounded from 60 to 3600.
Requester and approver have different fixed identities and minimal workflow
scopes. Neither gets connection-management privileges. The requester cannot
approve its own action, and the approver cannot assign or queue requester work.
Use separate browser sessions for convenience. The workstation owner can issue
both tokens: this is logical role separation for development, NOT independent
production identity or non-author security review. Session output is sensitive;
do not copy it into issues, commits, logs or shared screenshots.

Assign the built-in follow-up workflow and choose a future weekday time window
inside the synthetic calendar's office hours. Plan as requester, review and
approve the exact proposal as approver, then queue as requester. Planning and
approval alone never start a worker or create a calendar effect.

```sh
# One separately controlled worker pass, processing at most one queued action.
"$BIN" --workspace "$WORKSPACE" worker

# Optional explicitly bounded polling. No HTTP command can start this process.
"$BIN" --workspace "$WORKSPACE" worker --passes 30 --interval 1
```

Every new coordinated action still requires the pinned TAL runtime, valid source
binding and exact human approval. Worker results concern only the synthetic
provider. It does not need an available inference sidecar to execute previously
approved queued work. Repeated worker commands verify/reconcile the original
outbox identity rather than creating replacement inference attempts.

## Persistence and failure behavior

Owner-only files contain independently generated local broker, operator, workload
and dispatch keys, an authenticated workspace manifest, cooperative lock files,
and the SQLite database. No real provider credential is used. Restart retains
those keys, task references, manifests, proposals, approvals, cost holds and
synthetic provider effects. Startup rejects changed keys, missing state and
incompatible configuration instead of pretending to begin a new installation.

The API refreshes its advisory source from the durable synthetic provider before
workflow requests. Issued projections remain bound to their original source.
Queueing after an API restart reconstructs the original persisted observation;
it revalidates current references and both sources BEFORE publishing the exact
original projection. It does not extend the projection, inference or approval
expiry. Old work becomes stale when another action changes the source.

The launcher permits one API process and one worker process per workspace. A
nonblocking file lock serializes their supported state operations; a busy API
returns a fixed 503 instead of silently retrying. Kernel locks are released after
abrupt process exit. There is no blanket retry of planning or business effects.
Only the socket lifetime journal may clean up a proven stale inference socket.

The existing conservative admission policy remains in force, including four
outstanding invocations per tenant. Successful mock results and uncertain calls
can retain held capacity. Restart does not clear quotas or refund reservations.
When blocked, inspect the recorded state using the
[operator recovery console](W4_OPERATOR_RECOVERY.md). Eligible pre-effect tasks
can be [retired](TASK_RETIREMENT.md); replacement work requires a new task and
fresh authority. [Independent accounting reconciliation](INFERENCE_ACCOUNTING_RECONCILIATION.md)
can release proven settled capacity without recovering a model result or
authorizing a business action. There is no general operator refund/reset command.

## Acceptance and limitations

The dedicated workflow installs the hash-locked package, runs workspace/API
regressions, then starts actual CLI processes outside the checkout. Acceptance
uses real loopback HTTP, the real private UDS transport and a separately invoked
TAL-enforcing worker. It kills and restarts the API after planning, approves and
queues in the new process, executes without an inference sidecar, and creates a
second task against the updated durable provider. Tokens and local key material
must not be printed in test logs. Exact final results belong in the PR discussion;
tests that did not run must not be reported as passing.

This is a single-workstation SQLite development topology. All processes share
the owning UID, so filesystem checks and cooperative locks are not a sandbox
against that owner, root or malicious trusted code. Do not expose the API beyond
loopback, use real customer data, modify workspace internals while running,
combine it with old workers, or restore an old backup and blindly resume writes.
Production PostgreSQL deployment, separate OS identities, workspace-secret
rotation, vulnerability disposition, independent review and enforced merge
requirements remain separate release work. Supported offline schema upgrades,
authenticated backups/rollback and billing-keyring distribution are described in
[workspace maintenance](WORKSPACE_MAINTENANCE.md); normal startup does not perform
them automatically. Restoring a backup is not evidence that an external effect
did not occur.

**Live Google acceptance remains BLOCKED; hosted inference and production
activation remain disabled.**
