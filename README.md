# TERVORO SAFE-AGENT

A contract-first, privacy-minimized foundation for an autonomous office agent.

**Status (October 15, 2026): M0–M6 and the offline M7-01–04 stack are
integrated into main (PRs #25–28 merged in dependency order).**
M7-05a adds invocation identity, pure lifecycle replay and the 0010 forward
schema foundation. Runtime durable dispatch/recovery is not wired yet.
**M5 LIVE ACCEPTANCE: BLOCKED.** The Google test recovery worker remains
MockTransport-only. No actual hosted Google event write or independently
verified live readback has been approved. The code is not production-approved;
external security review and main-branch protection are still outstanding.

## Architecture and milestones

- [System design](docs/SAFE_AGENT_DESIGN.md)
- [MVP implementation plan](docs/MVP_IMPLEMENTATION_PLAN.md)
- [M7-05a invocation foundation and next increment](docs/M7_DURABLE_INFERENCE_DISPATCH.md)
- [M1 delivery and reproducibility](docs/M1_DELIVERY.md)
- [M2 delivery, limitations and verification](docs/M2_DELIVERY.md)
- [M3 verified synthetic scheduling and limitations](docs/M3_DELIVERY.md)
- [M3 synthetic execution decision](docs/adr/0004-verified-synthetic-execution.md)
- [M2 authority and ledger decision](docs/adr/0003-office-authority-ledger.md)
- [M1 architecture/security decision](docs/adr/0002-virtual-office-simulation.md)
- [M0 foundation decision](docs/adr/0001-mvp-scope.md)
- [Upstream TAL compatibility findings](docs/tal-compatibility.md)
- [Security conformance coverage](docs/security-test-matrix.md)
- [M7-01 inference contracts and offline mock](docs/M7_INFERENCE_CONTRACTS.md)

Upstream TAL: [TervoroAgenticLanguage-TAL-](https://github.com/AWDaley/TervoroAgenticLanguage-TAL-)
(historical M0 baseline `23d48c4a4c66ab4d7ef722794f755f0bfcd753d4`).
The **current exact upstream source revision** is pinned once in
[`ci/tal-office-commit.sha`](ci/tal-office-commit.sha), and both cross-repo
CI jobs install it. The TAL office runtime/execution record is experimental,
trusted-only and **not** an executable bearer capability or live certification.

## Local setup

Use Python 3.12+ from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
pytest -q
ruff check src tests scripts migrations
mypy src/safe_agent
python scripts/export_schemas.py --check
python scripts/export_m1_schemas.py --check
python scripts/export_m2_schemas.py --check
python scripts/export_m3_schemas.py --check
```

`requirements.lock` pins direct packages only, not their full transitive closure.
A reviewed full lockfile is still a reproducibility follow-up.

## Explore the Virtual Office

```bash
python -m safe_agent virtual-office-demo
python -m safe_agent demo                 # original M0 compatibility demo
```

The M1 demo loads private **synthetic** records into the trusted domain layer,
mints scoped, non-bearer handles, computes free/busy slots, generates a fake
planner proposal, and produces a read-only `CalendarSimulation`. The output is
safe for a synthetic demonstration: no raw customer names or emails, no
provider identifiers, and an explicit `SIMULATED_ONLY` state.

The internal task-list simulator is also available to Python callers but
`tasks.create` is **not** a TAL-authorized effect. Both simulators reject
stale/altered projections, enforce typed inputs, and report conflicts.

## Security boundaries

- Closed immutable Pydantic model contracts; schema export is versioned.
- Handles are validated on every broker resolution for organization, actor,
  task, operation, resource kind, expiry and revocation. Merely knowing one
  never grants authority.
- The planner receives only sanitized availability data and references.
  Private synthetic customer information is never serialized to it.
- Simulations are advisory, not holds/reservations or completion evidence.
- The genuine upstream TAL adapter **still denies all provider execution**.
  M2's office gate can only deem synthetic proposals eligible; no model-to-provider
  path exists.
- Python module boundaries are not process sandboxing. Trusted components
  require separate identities and enforced egress restrictions before real
  providers or untrusted model code may be connected.

For dev database only: `docker compose up -d postgres` starts a local test
PostgreSQL instance; M1 does not use it. Never apply synthetic credentials or
fixtures to real business systems.

**M3 milestone:** A trusted offline gateway now performs fake calendar and
internal-task creation, independently verifies read-back, and emits verified
completion evidence. M4 must provide crash-safe outbox/reconciliation. Upstream
TAL office-domain integration remains mandatory before any real provider.


## M2 — Trusted offline policy and durable evidence (synthetic only)

M2 introduces SQLAlchemy-backed task and manifest registration, a provisional
TAL-style office policy gate, proposal-specific approvals, a hash-linked event
ledger with pure replay, HMAC-protected private broker mapping snapshots, and an
authenticated **local-only** operator API. The agent has no provider tool access.

```bash
python -m pip install -e '.[dev,api]'
python -m safe_agent m2-demo
pytest
```

The demo reaches `READY_TO_COMMIT` in an **offline policy simulation** and
reports zero external effects. This is **not a real authorization or booking**:
upstream TAL v0.3's calendar domain is still unimplemented. See
[`docs/M2_DELIVERY.md`](docs/M2_DELIVERY.md) and
[`docs/adr/0003-office-authority-ledger.md`](docs/adr/0003-office-authority-ledger.md).


## M3 — Verified synthetic office execution

```bash
python -m safe_agent m3-demo
```

The demo uses one synthetic calendar and customer, a deterministic fake planner,
scoped references, a trusted offline approval/eligibility gate, a versioned fake
calendar writer, and independent read-back. It prints the before/after booking
count, action evidence receipt, `COMPLETED` snapshot, and hash-linked timeline.
The same trusted gate additionally supports synthetic `tasks.create` under an
explicit internal-task manifest. Optional `ClosedJsonPlanner` accepts a text-only
model callback with sanitized context; it exposes **no direct connector tools**.
See [`docs/M3_DELIVERY.md`](docs/M3_DELIVERY.md) for test and security limits.

**No live writes:** the runtime rejects any adapter except its specific fake
classes. The upstream TAL calendar domain pack, resilient outbox, real-provider
read-back, and production sandbox are intentionally not present.

## M4: durable synthetic recovery

The M4 configuration durably queues trusted office intents, dispatches exclusively
through `SyntheticRecoveryWorker` with expiring leases, and reconciles uncertain
synthetic effects using the fake provider's persisted action-key registry. The
existing in-memory M3 demo remains intentionally non-recoverable. The new M4
demo injects a post-commit crash and restarts the ledger, provider and broker:

```bash
python -m safe_agent m4-demo
```

See [`docs/M4_DELIVERY.md`](docs/M4_DELIVERY.md) for the fault matrix,
operator-facing outbox status (`/v1/tasks/{task_id}/execution`), and the remaining
upstream TAL / real-provider constraints. **No production connector is enabled.**

## M5: isolated Google Calendar connector (offline-tested, no live writes)

The `safe_agent.integrations.google_calendar` package now supplies a Google Calendar test-account OAuth2 PKCE flow, an encrypted server-side refresh-token vault with an explicit test-calendar allowlist, sanitized free/busy observations, and a trusted low-level deterministic event insert/readback API exercised only against an HTTP simulator. **The agent cannot send model-proposed writes to Google Calendar:** upstream TAL office-domain certification and live test-account acceptance have not been completed. No Google credentials are included or required to run the offline tests.

```bash
python -m safe_agent m5-demo
pytest -q tests/m5
python scripts/export_m5_schemas.py --check
```

See [M5 delivery/limitations](docs/M5_DELIVERY.md), [test-account setup](docs/M5_GOOGLE_TEST_SETUP.md), and [ADR 0005](docs/adr/0005-google-calendar-test-boundary.md). Do not use a production/personal calendar or put client secrets, provider IDs or refresh tokens in agent context or source control.

## PR3: offline Google Calendar readback and recovery (not live)

The privileged OfflineGoogleTestRecoveryWorker uses HTTP MockTransport ONLY
and cannot reach Google services. It requires the TAL-bound outbox, exact
private calendar mapping and post-write independent readback; ambiguous effects
are reconciled or escalated without blindly retrying inserts. See
[offline recovery limitations](docs/M5_GOOGLE_RECOVERY.md). Real Google writes
remain disabled; PR4 delivers offline acceptance-readiness checks, not a live test.

## PR4: provider-sourced simulation and read-only acceptance preflight

The stacked PR4 branch adds a provider-originated, hash-bound advisory simulation
for test Google outbox intents, plus a strictly read-only operator acceptance
preflight. It cannot authorize or perform live Google inserts. See
[PR4 acceptance readiness and blockers](docs/M5_ACCEPTANCE_READINESS.md).
**M5 live acceptance remains BLOCKED** until a disposable account, reviewed
worker and independently verified event-readback evidence are supplied.

## PR5: consistent TAL pin and explicit M5 release decision

The canonical TAL commit is in [`ci/tal-office-commit.sha`](ci/tal-office-commit.sha).
Both GitHub workflows install this exact revision after validating its hash;
they run the offline conformance suite. The release gate distinguishes
**offline CI configuration** from the missing actual Google test-account
acceptance:

```bash
python scripts/read_tal_pin.py
python scripts/check_m5_release.py --mode offline  # verifies repository wiring
python scripts/check_m5_release.py --mode live     # exits nonzero; not accepted
```

The offline gate does **not** itself run tests; rely on the CI job outcomes.
A passing offline gate never marks live Google writes or production authorized.
Read the [M5 release decision and merge order](docs/M5_RELEASE_GATE.md) and
[operator acceptance requirements](docs/M5_ACCEPTANCE_READINESS.md).

## M6 operator workspace, task intake and offline preparation

The optional local operator interface at /operator exposes task inbox, stored
simulation preview, exact-proposal approval or denial, and safe audit evidence.
M6-01b adds an opt-in TrustedTaskProvisioner that creates server-authorized
synthetic calendar or internal-office tasks from a trusted, actor-scoped
workflow catalog. POST /v1/tasks never accepts a client-authored manifest,
grant, raw provider identifier, or external connection. The new Alembic
revision 0007 persists initial task and private reference bindings atomically,
with SQL idempotency for response-loss retries.

Task assignment alone neither plans nor performs an action. M6-01c adds an
opt-in deterministic synthetic preparer that atomically records a Virtual Office
simulation and TAL-style locally gated proposal, plus a strictly queue-only
synthetic outbox route. A separately hosted trusted SyntheticRecoveryWorker
must claim and independently verify the fake effect. The browser cannot
dispatch provider actions; Google live writes are still prohibited.

M6-01d adds an optional SupervisedSyntheticWorker that polls already-queued
synthetic intents with bounded claim batches, interruptible stop signals,
backoff and halt-on-infrastructure-failure behavior. It delegates all
authority and readback decisions to the existing SyntheticRecoveryWorker;
the operator HTTP API still never dispatches effects. Run the isolated
demo with: python -m safe_agent m6-worker-demo.

M6-01e adds a durable, task-scoped, versioned planning workspace with a
strict 3-attempt default budget, expiring planning leases and safe
clarification/replan outcomes. An optional ClosedJsonPlanner callback
receives only sanitized task context and availability, never tools, private
source IDs, credentials, or authority to execute. A proposal and its
planning success outcome commit atomically under the trusted policy gate.
Run the in-process protocol-stub demo with: python -m safe_agent
m6-planning-demo. This synthetic test stub is NOT an isolation boundary.

M6-01f adds an explicit Docker-only isolated text-model transport: a locally
pinned image ID, network=none, read-only filesystem, no host mounts or
inherited credentials, dropped capabilities, strict time/byte/token-proxy and
estimated cost ceilings. The model result still passes the same trusted
proposal/approval/worker gates. Run the no-network Docker demo with:
python -m safe_agent m6-isolated-model-demo (requires an audited local image
and SAFE_AGENT_ISOLATION_IMAGE_ID). This does not connect a hosted LLM.

Operators continue to use trusted local test tokens only. Deployment without
the optional provisioner/workflow components remains review-only. See
docs/M6_OPERATOR_INBOX.md, docs/M6_TASK_INTAKE.md and
docs/M6_SYNTHETIC_WORKFLOW.md, docs/M6_SUPERVISED_SYNTHETIC_WORKER.md
docs/M6_PERSISTENT_PLANNING.md and docs/M6_ISOLATED_MODEL_RUNTIME.md.

## M7-01 — Inference-only contracts (offline mock)

A trusted, versioned InferenceRoutePolicy chooses a narrow calendar
proposal/clarification capability and caps response bytes, time,
conservative token proxies and estimated cost. The new InferenceGateway
accepts only canonical, sanitized task/projection input, returns typed
proposal data (never execution authority), and supports a deterministic
no-network OfflineMockInferenceTransport. A future separately reviewed
gateway may add isolated model-provider transport and verified billing.
See docs/M7_INFERENCE_CONTRACTS.md.

M7-02 adds an opt-in restricted HTTPS transport for a separately hosted,
inference-only sidecar. It requires a trusted fixed FQDN/path, rejects
private and mixed DNS answers, connects to the checked numeric IP with
CA-verified hostname/SNI, disallows redirects, and bounds response bytes.
Inference credentials must be held outside the operator process and model
container; this change does not connect a live model provider.
See docs/M7_RESTRICTED_EGRESS.md.

M7-03 adds opt-in SQL-backed, global/tenant/task inference cost ceilings
using integer micro-USD, atomic reservations BEFORE any metered backend
call, hash-linked budget evidence, and fail-closed indefinite holds on
uncertain charges. Only the exact offline mock can settle at zero without
external billing evidence; a separate trusted billing verifier may supply
one scoped HMAC attestation to reconcile reserved cost. Provider-reported
token counts never count as verified billing. The legacy unbudgeted
M7 test interface remains available for synthetic fixtures ONLY; no
hosted inference is production-approved. See docs/M7_INFERENCE_BUDGETS.md.

M7-04 adds a separately launched, offline-only inference ASGI service
over an owner-restricted Unix socket. It requires a trusted workload
Bearer token, rejects replayed or out-of-scope requests, enforces
bounded concurrency, rate and response limits, and reports sanitized
operational status. A separate **synthetic** billing source verifier
issues scoped attestations only for source-signed offline evidence.
Neither component connects a live model, verifies a real provider
invoice, or gains business-system authority. Run
python scripts/smoke_inference_sidecar.py; see
docs/M7_INFERENCE_SERVICE.md.

## M7-05a — Durable invocation schema and replay foundation

Closed invocation bindings connect one planning attempt to an exact task,
manifest and budget reservation. The additive `0010_inference_dispatch`
migration preserves canonical PR #27 accounting, rejects duplicate/cross-scope
bindings and adds PostgreSQL immutable history/transition guards. Pure replay
rejects tampering, invalid state transitions and late successful results.
No prompt, raw completion or second money ledger is stored.

This is the first increment of M7-05, **not** a runtime at-most-once dispatch
or restart-recovery implementation. Existing inference and sidecar behavior
remain unchanged. Run `python scripts/export_m7_dispatch_schemas.py --check`
and read [delivery, tests and M7-05b](docs/M7_DURABLE_INFERENCE_DISPATCH.md).

**M5 LIVE ACCEPTANCE: BLOCKED.** Passing offline tests or generating a
synthetic proposal never authorizes real provider execution or production
deployment.

## M7-05b: atomic inference admission (opt-in, no dispatch)

The trusted `TrustedInferenceAdmission` API atomically binds a real planning
attempt, one stable inference request ID, the existing budget reservation and
persistent global/tenant/task admission quotas. Exact repeats return the same
identity without another hold. Forward migration `0011_inference_admission`
preserves both canonical predecessors. No prompt or raw response is stored.

This is an admission-only increment: it does **not** yet wire the running
planner/sidecar through an at-most-once dispatcher. Expired or uncertain
invocations retain conservative capacity. See
[atomic admission and limitations](docs/M7_ATOMIC_INFERENCE_ADMISSION.md).
**M5 LIVE GOOGLE ACCEPTANCE remains BLOCKED; no hosted model is enabled.**

## M7-05c — opt-in fenced offline inference dispatch

`TrustedInferenceCoordinator` now joins durable admission to a fenced,
commit-before-send invocation marker and a coordinator-authenticated private
Unix-socket sidecar request. Caller crashes and lost replies never silently
resend a started invocation; uncertain work retains its cost hold and outstanding
capacity. Coordinated mode disables the raw `/v1/infer` endpoint. Ownership-journal
socket recovery rejects unowned stale paths. No historical migrations change.

Run `python scripts/smoke_inference_coordinator.py` for the synthetic caller-crash
and sidecar-restart acceptance. See [M7 coordinated dispatch](docs/M7_COORDINATED_DISPATCH.md)
for exact trust boundaries and remaining M7-05d/e work. Existing operator planning
routes are not yet wired to the coordinator. **M5 LIVE GOOGLE ACCEPTANCE: BLOCKED.**
