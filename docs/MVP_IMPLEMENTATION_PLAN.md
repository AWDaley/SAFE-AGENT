# TERVORO SAFE-AGENT — MVP Implementation Plan

**Version:** 1.0 — Implementation proposal

**Date:** 2026-10-13

**Repository:** https://github.com/AWDaley/SAFE-AGENT

**Companion:** [SAFE_AGENT_DESIGN.md](SAFE_AGENT_DESIGN.md)

**Status (updated October 14, 2026):** This document began as the pre-implementation
design; M0–M4 and the offline M5 foundation are now in main, and M6 task
intake, workflow, worker supervision, bounded text planning and Docker isolation
have also been merged via PRs #20–24. M7-01 adds the first inference-only
contract and mock. Sections describing the initial empty repository remain
historical. Real Google acceptance and production release remain BLOCKED.

> **Delivery objective:** Demonstrate one officekeeping task end to end: a persistent single agent uses a permission-filtered Virtual Office to schedule a follow-up appointment for a synthetic customer, proposes exactly one typed operation per TAL cycle, passes an independent deterministic authority gate, commits via a trusted adapter, reconciles uncertainty, and reports completion only from verified external state. Once this works in a fake environment, demonstrate it against a deliberately isolated Google Calendar test account.

---

## 1. Starting point and discovery gate

At the time this plan was drafted, the public `main` branch of `AWDaley/SAFE-AGENT` displayed a `README.md` but no application source. This is a new implementation effort rather than a refactor of verified working SAFE-AGENT code. **Do not assert that TAL v0.3 is implemented until Codex inspects an actual local or linked TAL codebase.** TAL is an architectural compatibility baseline in the design document, not evidence of a currently importable package.

Before coding:

1. Fetch and inspect the current default branch, commit history, README, existing configuration, and any recently added code. Record the commit SHA in the M0 notes. Do not overwrite concurrent changes.
2. Search accessible repositories and provided artifacts for an existing TAL v0.3 implementation, canonical schema fixtures, hash/canonicalization rules, manifests, event ledger, reducer, verifier, and action-gate interfaces. Record **verified implementation**, **published contract only**, or **not available** for each component.
3. If an existing TAL implementation is verified, reuse it behind a thin adapter and run its compatibility fixtures. If none is available, implement the **minimum locally tested TAL-compatible boundary** needed by the MVP, label it as a provisional implementation, and avoid declaring protocol compatibility until canonical fixtures can be checked.
4. Freeze the first MVP task definition, risks, provider capability assumptions, selected dependency versions, and completion conditions in an architecture-decision record (ADR).
5. Open development on a feature branch, use reviewable milestones/PRs, and keep `main` deployable. Prefer additive changes; do not rewrite the design specification during implementation without a separate decision record.

### Definition of a successful MVP demonstration

An authenticated operator chooses an authorized customer handle and says, for example, “Book a 30-minute follow-up during tomorrow afternoon.” The model (or deterministic test planner) reads only sanitized availability and scoped references; the Virtual Office shows a preview; the agent submits a closed-schema proposal; TAL checks the pinned task contract, identity, permissions, time window, resource scope, approval requirements, and budget; a trusted calendar adapter performs the write; a separate verifier reads the resulting event; the event ledger captures the complete chain; and the UI says **Completed** only after the expected postconditions are supported by evidence. The same flow must tolerate stale availability, a denied operation, and an uncertain network result without duplicate appointments.

### Scope guardrails

**In scope:** single organization, one agent/task at a time initially, deterministic fake calendar + synthetic customers and tasks, durable lifecycle, typed plans/proposals, Virtual Office previews, authorization, trusted reference resolution, append-only evidence, bounded retries, conflict recovery, manual approval, basic dashboard/CLI, and one test-calendar integration.

**Out of scope:** generic browser/computer control; arbitrary SQL; reading free-form private inbox contents; arbitrary outbound email; full clones of provider UIs; multiple cooperating agents; multi-tenant SaaS production launch; payments; destructive operations; deployment for regulated data; and unsupervised arbitrary plugin/tool installation.

## 2. Locked architectural rules

The following are release blockers, not aspirational guidance:

- **Authority:** natural-language task text, retrieved content, model output, and model memory never grant or expand permissions. Effective authority comes from a version-pinned manifest compiled by trusted code under an authenticated initiating principal.
- **Single action gate:** one closed-schema model proposal per TAL cycle and **at most one externally effective operation per cycle**. A second effect requires a distinct authorized cycle. The model has no direct path to connectors, shell, production network egress, or credentials.
- **Simulate versus commit:** the Virtual Office is a permission-filtered, versioned working projection. Its success is advisory. Before commit, trusted code checks current provider state and action preconditions again; source applications remain authoritative.
- **Information isolation:** model-visible customer/event handles are opaque scoped references, not raw identifiers or bearer authorizations. Resolution requires independent tenant, task, actor, scope, operation, and expiry checks. Sensitive values and OAuth tokens never enter prompts, model memory, traces, or ordinary logs.
- **Deterministic records:** manifests, proposals, decisions, and event payloads have versioned schemas and stable canonical hashes. Evidence is append-only; derived task state is reproducible from recorded events without re-running the model or connector.
- **Fail closed:** invalid schema, unknown action, missing grant, expired approval, stale version, and ambiguous outcome cannot silently authorize or become success. A rejected proposal causes zero external effects.
- **Completion:** the model cannot mark its own task complete. A trusted verifier checks postconditions against independent provider state and records its receipt.
- **Recovery:** dispatch uncertainty enters an explicit `OUTCOME_UNKNOWN`/reconciliation path. Never blindly retry a potentially executed provider write. Use idempotency/reconciliation where possible and escalate unresolved ambiguity.
- **Separation of environments:** tests, shadow-policy runs, and fault injection cannot operate on live business systems. Production/provider credentials are not present in the model worker.

## 3. Technology choices and boundaries

Use a **modular Python monolith** rather than multiple microservices for the first vertical slice. Modular does not mean that the model and privileged executor share authority: they must have separate logical interfaces, independent credentials, and enforceable process/network restrictions before a real provider is enabled.

| Concern | MVP choice | Notes |
|---|---|---|
| Language and packaging | Python 3.12+, `pyproject.toml`, `uv` or standard `pip` | Pin versions and lock dependencies. |
| API/backend | FastAPI + Pydantic v2 | Strict request validation; forbidden extra fields. |
| Persistence | PostgreSQL, SQLAlchemy 2.x, Alembic | Durable tasks, manifests, outbox, approvals, ledger. SQLite may be used for pure unit tests only. |
| Model integration | Provider-neutral `Planner` protocol + deterministic fake planner | Fake planner runs entire flow before model API is introduced. |
| Virtual Office | Typed Python domain projection + deterministic simulator | Synthetic calendar/customer/task fixtures; no external writes. |
| TAL | Existing TAL v0.3 core if verified; otherwise minimal provisional implementation behind compatibility API | Never maintain competing authority gates. |
| Effect execution | Privileged adapter/service interface | Fake adapter first; one sandbox/test Google Calendar adapter later. |
| Concurrency and workers | PostgreSQL-backed outbox and single worker | Transactional intent recording; explicit leases and recovery. |
| Frontend | Small React/TypeScript UI or server-rendered operator view | Do not delay backend conformance to build a polished UI. |
| Tests and CI | pytest, hypothesis (selected invariants), Ruff, type checking, GitHub Actions | Mocks and fault injection; no secrets in CI. |
| Development | Docker Compose for backend + PostgreSQL + fake adapters | One-command local demonstration, seeded fixtures. |

### Repo layout (target, not an assertion about existing files)

```text
SAFE-AGENT/
├── README.md
├── docs/
│   ├── SAFE_AGENT_DESIGN.md
│   ├── MVP_IMPLEMENTATION_PLAN.md
│   ├── adr/
│   ├── tal-compatibility.md
│   └── security-test-matrix.md
├── src/safe_agent/
│   ├── api/                 # task, approval, status, audit HTTP APIs
│   ├── agent/               # planner protocol, typed sanitized context
│   ├── orchestration/       # task state machine, cycle budget, worker
│   ├── virtual_office/      # customer/calendar/task projections, simulator
│   ├── tal/                 # sole manifest/gate/reducer integration boundary
│   ├── broker/              # scoped references + privileged resolution
│   ├── adapters/            # fake, later Google Calendar (privileged)
│   ├── execution/           # outbox, idempotency, reconciliation, verifier
│   ├── evidence/            # events, hash chain, replay
│   ├── storage/             # DB models, repositories, migrations
│   └── security/            # principal auth, scopes, redaction
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── conformance/
│   ├── adversarial/
│   ├── recovery/
│   └── acceptance/
├── fixtures/synthetic_office/
├── web/                     # small operator UI in later milestone
├── compose.yaml
├── .env.example             # names only, no credentials
└── .github/workflows/ci.yml
```

Make interfaces stable before adding business integrations. Avoid broad abstractions, premature event-bus infrastructure, agent swarms, full-blown workflow DSLs, and background components whose behavior cannot be deterministically tested.

## 4. MVP workflow and internal contracts

### 4.1 Task and authority contracts

`Task` has `task_id`, `organization_id`, `initiator_id`, objective, selected `manifest_id/hash`, bounded budget, state, and deadline. Initial task creation may use an explicit structured scheduling form or safely constrained natural language. The trusted API creates authority; the agent does not choose its permissions.

`TaskManifest` is immutable and versioned. Minimum policy: permitted operation `calendar.create_event`, selected calendar scope, one allowed customer reference, start/end business-hours window, maximum duration 60 minutes, target time zone, approval thresholds, model/tool budgets, retry ceiling, and evidence requirements. A manifest change creates a new version, not an edit in place.

`ScopedReference` contains an unguessable handle bound to the organization, initiating principal/task, allowed operations, resource mapping, manifest/policy version where appropriate, and expiry. Handle resolution is solely broker-side. Do not treat knowledge of a handle as sufficient authorization.

`Projection` carries sanitized domain fields, originating connector, revision, fetched timestamp, freshness deadline, scope, and classification label. A stale projection may be refreshed for planning, but cannot authorize a commit.

`Simulation` stores its projection revision, change-set preview, assumptions, conflicts, expected postconditions, and stable hash; it is **never** a provider-side reservation.

### 4.2 Closed proposal example

```json
{
  "schema_version": "safe_agent.proposal.v1",
  "operation": "calendar.create_event",
  "calendar_ref": "calendar_scope_01",
  "customer_ref": "customer_ref_a9",
  "start_at": "2026-10-15T14:00:00-04:00",
  "end_at": "2026-10-15T14:30:00-04:00",
  "time_zone": "America/New_York",
  "template_id": "follow_up_v1",
  "simulation_ref": "simulation_8b",
  "projection_rev": "projection_42"
}
```

The JSON is illustrative. Closed Pydantic schemas must reject unknown operations and fields. Identity, manifest hash, provider credentials, approved source system identifiers, and idempotency keys are supplied by trusted code—not the model. The canonical proposal hash binds approvals to exact action parameters.

### 4.3 Minimal application contracts

- `Planner.plan(safe_task_context, safe_projection) -> ProposedAction | RequestClarification` (no privileged tool handles).
- `VirtualOffice.observe(scoped_request) -> Projection` and `.simulate(projection, change_set) -> Simulation` (side-effect free).
- `TAL.validate(task, proposal, current_state) -> Decision` (closed, deterministic, signed/hash-bound to the manifest).
- `ExecutionGateway.commit(approved_action) -> Committed | Failed | OutcomeUnknown` (the only provider-write path).
- `Adapter.get_state/read_back/find_effect` return enough trusted evidence for verification, with capability declarations for conditional operations and idempotency.
- `Verifier.verify(postconditions, trusted_evidence) -> Verified | Unverified | Conflict` (independent from model narration).
- `EventReducer.reduce(previous_state, event) -> next_state` (pure, deterministic, replayable).

Do not allow a generic `execute_command`, `make_http_request`, or `run_sql` escape hatch in model-visible operations.

### 4.4 Key task states

`ASSIGNED -> PLANNING -> SIMULATED -> VALIDATING -> (REJECTED | AWAITING_APPROVAL | READY_TO_COMMIT) -> EXECUTING -> (VERIFIED | FAILED | CONFLICT | OUTCOME_UNKNOWN)`; `OUTCOME_UNKNOWN -> RECONCILING -> (VERIFIED | FAILED | NEEDS_REVIEW)`. Rejected and conflicting tasks can return to planning only within explicit budgets and retry rules. A task is `COMPLETED` only after its required verified postconditions have been checked, and all required actions have terminal evidence.

### 4.5 Local operator API

- `POST /tasks` — create permitted scheduling task under authenticated user (task-specific authority bound server-side).
- `GET /tasks/{task_id}` — safe current status and verified outcomes.
- `GET /tasks/{task_id}/preview` — sanitized proposed delta + simulation assumptions.
- `POST /tasks/{task_id}/approvals` — decision bound to canonical proposal hash, approver, time, and expiry.
- `GET /tasks/{task_id}/events` — redacted, permission-filtered audit timeline.
- `GET /health` — readiness/liveness (never leaks configuration secrets).

The fake-planner demo may additionally expose a CLI command that submits and processes a seeded task. Security checks must live below both CLI and HTTP surfaces.

## 5. Ordered implementation milestones

The milestones below are **merge boundaries**. Complete and test each before starting the next dependent boundary. Within a milestone, independent tasks may run in parallel, but neither the model nor new business code may circumvent the one authority gate.

### M0 — Repository foundation and TAL contract discovery

**Goal:** A reproducible, testable project with explicit authority contracts and no unsupported claims about preexisting TAL code.

**Tasks**

- [ ] `M0-01` Verify current repo tree and branch; identify any code added since this plan. Capture baseline SHA, risks, and relevant assumptions in `docs/adr/0001-mvp-scope.md`.
- [ ] `M0-02` Inspect any separately available TAL core. Write `docs/tal-compatibility.md` listing exact located source paths/versions, implemented interfaces, and missing contracts. Record whether import/reuse is feasible.
- [ ] `M0-03` Create Python package, dependency lock, Ruff, pytest, type checker, `.env.example`, Compose PostgreSQL, and CI. No runtime secrets committed.
- [ ] `M0-04` Define strict Pydantic schemas for task, authority manifest, scoped reference, projection, simulation, one-cycle proposal, action decision, approval, execution receipt, and evidence event. Version schemas and disallow additional fields.
- [ ] `M0-05` Implement stable canonical serialization/hashing to the actual TAL contract if available; otherwise document provisional behavior and add a compatibility fixture gate.
- [ ] `M0-06` Build deterministic in-memory `FakePlanner` and a minimal `TaskRunner` shell; do not connect real model/provider yet.

**Artifacts:** installable package, contract tests, local bootstrap instructions, CI passing, TAL compatibility matrix, ADR.

**Exit gate:** `pytest` and static checks pass from a clean checkout; malformed/unknown proposals fail closed; no model worker can import or invoke a privileged write API through its public interface. Any TAL compatibility gaps are explicit and cannot be presented as conformance.

### M1 — Synthetic Virtual Office and simulation

**Goal:** A useful, deterministic planning environment with zero provider side effects.

**Tasks**

- [ ] `M1-01` Create seeded fixtures: two calendars, working hours, at least five synthetic customers, existing bookings, appointment templates, and internal tasks. Include overlapping slots, DST transitions, and missing data.
- [ ] `M1-02` Define typed calendar/customer/task domain projections. Include source revisions, freshness, provenance, allowed fields, and redaction labels.
- [ ] `M1-03` Build broker-minted scoped references: tenant/actor/task/operation/expiry checks; protected mappings stored outside model-visible data structures.
- [ ] `M1-04` Implement `observe()` and `simulate()` for `calendar.create_event`; produce change diff, expected postconditions, conflict warnings, simulation hash, and source revision.
- [ ] `M1-05` Add `tasks.create` as an internal synthetic operation only after calendar simulation is tested.
- [ ] `M1-06` Add deterministic tests for read-only simulation, scope leaks, stale projections, business hours, overlap detection, and timezone/DST ambiguity.

**Artifacts:** Virtual Office services, fixtures, preview JSON, simulation tests.

**Exit gate:** identical inputs yield identical simulation output; simulation writes no authoritative provider data; model context contains no synthetic raw name/email/secret fields; a revoked or cross-tenant handle is rejected by the broker.

### M2 — Deterministic TAL action gate, persistence and audit

**Goal:** Independently enforce authority and derive task status from durable events.

**M2 delivery (October 13, 2026):** Implemented synthetic-only manifest pinning, provisional office policy, approvals, PostgreSQL schema/migration, and a replayable ledger. **The upstream TAL v0.3 calendar domain and provider dispatcher are not implemented; a positive offline eligibility result is not permission to write.** See `docs/M2_DELIVERY.md`.

**Tasks**

- [x] `M2-01` Create immutable manifest registration/pinning and task-grant compilation (reuse verified TAL core if present).
- [x] `M2-02` Implement one typed proposal per cycle; reject extra fields, unknown actions, malformed references, unauthorized resources, parameter-range violations, expired budgets, and stale or mismatched proposal state.
- [x] `M2-03` Add action-specific approval requests; bind approval to exact canonical hash, approver identity, allowed authority, and expiry. Changed proposals invalidate approvals.
- [x] `M2-04` Create PostgreSQL schema/migrations for tasks, manifest versions, references, projections, simulations, proposals, approvals, execution records, and append-only evidence events.
- [x] `M2-05` Implement hash-linked event append and pure reducer; replay must reproduce final state without contacting an LLM or provider.
- [x] `M2-06` Connect `TaskRunner` to TAL validation; only authorized proposals can reach an effect dispatcher. Rejections must not enqueue effects or reserve effectful idempotency keys.
- [x] `M2-07` Add tenant/actor authorization at API and broker boundaries, redacted structured logs, and provenance metadata.

**Artifacts:** deterministic gate, approval service, event ledger, reducer, migrations, policy fixtures.

**Exit gate:** all denied-operation tests yield zero external effects; byte-stable replay on pinned fixtures; changed or expired approvals fail closed; one proposal/cycle and no more than one effect/cycle are enforced by tests.

### M3 — Verified synthetic end-to-end scheduling

**Goal:** Complete the first useful workflow in a fake environment without any real calendar account.

**Tasks**

- [ ] `M3-01` Implement a privileged `FakeCalendarAdapter` with versioned calendar state, separate read/write interfaces, and an effect counter.
- [ ] `M3-02` Implement a single trusted execution gateway: precommit read, calendar/time constraints, approved reference resolution, stable action identity, provider adapter invocation, postcondition verification.
- [ ] `M3-03` Create a deterministic `FakePlanner` happy path: availability -> preview -> typed proposal -> TAL decision -> fake adapter commit -> verified read-back -> completion.
- [ ] `M3-04` Add a configurable model adapter that is restricted to sanitized planning messages and closed proposal output. Do not expose a direct connector tool. Keep fake planner selectable for reproducible tests.
- [ ] `M3-05` Implement one internal task-creation flow under the same gate, demonstrating domain extensibility without a second authority plane.
- [ ] `M3-06` Add a task CLI or simple HTTP test client that prints a safe preview and verified evidence chain.

**Artifacts:** working synthetic vertical slice, typed planner interface, fake-provider conformance tests, demo script.

**Exit gate:** a clean local run can schedule one synthetic follow-up, show a before/after preview and task timeline, and independently prove the calendar state change; the model cannot cause completion merely by stating it succeeded.

### M4 — Durable outbox, idempotency and failure recovery

**Goal:** Remain correct when state changes, calls time out, workers crash, and approvals expire.

**Tasks**

- [ ] `M4-01` Write execution intent and outbox record in one transaction before dispatch; dispatch only from trusted worker using bounded leases and state transitions.
- [ ] `M4-02` Persist action IDs, canonical proposal/manifest hash, idempotency state, provider effect references, and safe receipts. Declare which adapter operations support provider-native idempotency.
- [ ] `M4-03` Introduce `OUTCOME_UNKNOWN` and reconciliation routines (`find_effect`, safe provider read-back, bounded escalation). Never duplicate a write solely because response was lost.
- [ ] `M4-04` Simulate external concurrent changes, out-of-date projections, unavailable time slots, provider rate limits, and stale approvals; refresh/replan only under existing authority.
- [ ] `M4-05` Fault-inject process death before dispatch, during external call, and after provider success but before receipt persistence. Restart and replay from durable events.
- [ ] `M4-06` Add operator-facing `NEEDS_REVIEW` escalation for cases where duplicate prevention cannot be established.

**Artifacts:** worker/outbox, reconciliation and recovery service, fault-injection harness, recovery report.

**Exit gate:** in tested scenarios, no duplicate appointment after crash/retry; unknown effects never become silent success/failure; task state recovers from storage after restart; unresolvable cases are explicitly escalated.

### M5 — First real application integration (test calendar only)

**PR5 status (October 14, 2026): BLOCKED pending real Google acceptance.**
M5-01 through M5-03 have offline-tested implementation components in stacked
PRs #13–#16, but Google account consent, independent live event readback and
the required provider fault suite (M5-04) have **not** run. No unchecked task
may be marked complete on mock evidence alone. The cross-repo TAL pin and
blocking release command are documented in [M5_RELEASE_GATE.md](M5_RELEASE_GATE.md).
The M5 exit gate below is NOT satisfied.


**Goal:** Validate end-to-end behavior against an actual provider while retaining data isolation and TAL enforcement.

**Tasks**

- [ ] `M5-01` Implement Google Calendar adapter behind the existing interface; enumerate precise supported read, create, version-check, and reconciliation capabilities. Do not claim the provider supports atomic scheduling if it does not.
- [ ] `M5-02` Implement admin-initiated OAuth with minimum viable scopes, encrypted/managed token storage, connection revocation, and separate service identities. The planner receives neither token nor raw OAuth response.
- [ ] `M5-03` Synchronize **only authorized** availability and relevant event metadata into sanitized projections. Recheck the source before write and read back the created event.
- [ ] `M5-04` Test normal commit, stale-state conflict, 401/revoked authorization, 429/rate limit, network timeout, reconciliation, and calendar API permission rejection against an isolated test calendar.
- [ ] `M5-05` Red-team any user-supplied event text that might enter the workflow. Treat it as untrusted data with no authority.

**Artifacts:** scoped Google Calendar adapter, connection workflow, provider capability matrix, sandbox integration report.

**Exit gate:** end-to-end schedule and independent read-back work in a designated test calendar without model-visible private credentials; no personal/production account is used in automated tests; known consistency limitations are documented.

### M6 — Operator UI, acceptance evaluation and packaging

**Progress note (October 14, 2026):** M6-01 includes the merged review-only
inbox and M6-01b's opt-in trusted synthetic task assignment. M6-01c adds
deterministic, atomic synthetic preparation, server-authorized outbox staging
and independent fake-worker readback acceptance tests. M6-01d adds an
opt-in bounded synthetic worker supervisor, safe shutdown and an isolated
offline CLI demo. M6-01e adds bounded text-only model planning with durable
workspace leases, clarification/retry records and privacy-minimized outcomes.
M6-01f adds a Docker-only, digest-pinned, no-network model transport with
explicit resource/cost-proxy ceilings and real container smoke coverage.
A hardened independent runtime host, model-image provenance, hosted inference
egress and billing integration, provider authentication, real Google acceptance,
durable alerting and production identity remain open. See M6_TASK_INTAKE.md,
M6_SYNTHETIC_WORKFLOW.md, M6_SUPERVISED_SYNTHETIC_WORKER.md,
M6_PERSISTENT_PLANNING.md and M6_ISOLATED_MODEL_RUNTIME.md.


**Goal:** Provide a practical demonstration that an office user can understand and evaluate.

**Tasks**

- [ ] `M6-01` Build task creation/inbox, status, Virtual Office preview, proposal-specific approval/deny, and evidence timeline.
- [ ] `M6-02` Build connection status, permission-scoped account disconnect, and safe audit/export views. Require application user authentication and policy checks.
- [ ] `M6-03` Create held-out synthetic task scenarios and run automated policy/recovery benchmarks with exact pinned versions; capture completion, false blocks, unauthorized effects, duplicate writes, cost, and human intervention.
- [ ] `M6-04` Document setup, threat model, local demo, backup/recovery, limits, OAuth test setup, and incident/approval workflows. Run dependency checks and repository secret scanning.
- [ ] `M6-05` Produce release checklist and demo showing happy path, unauthorized denial, stale projection conflict, lost response reconciliation, and human approval.

**Artifacts:** minimal dashboard, installation guide, test report, demo walkthrough, release candidate.

**Exit gate:** all conformance tests pass, user can observe and approve the complete lifecycle, and proposed MVP thresholds in Section 8 are met with no unresolved critical control bypasses.

### M7 — Provider-neutral inference and controlled model integration

The first M7 increment defines closed InferenceRoutePolicy, InferenceRequest,
InferenceTransportReply and InferenceOutcome contracts, an inference-only
transport interface and a deterministic offline mock. M7-02 adds a
restricted trusted-sidecar HTTPS egress adapter with DNS address checks,
numeric IP pinning, TLS hostname verification and strict response limits,
tested against synthetic wire/DNS mocks only. M7-03 introduces durable
micro-USD inference cost reservations, conservative billing-uncertain
holds, task/tenant/global limits and signed issuer assertions. A
separately reviewed external billing verifier is still missing. The model is not a
trusted actor and never receives credentials or software execution access.
See M7_INFERENCE_CONTRACTS.md, M7_RESTRICTED_EGRESS.md and
M7_INFERENCE_BUDGETS.md. Later gated increments will need externally
verified token/invoice evidence, held-out evaluation and production
runtime isolation review. No hosted model or live Google provider write
is enabled by this prototype.

## 6. Suggested PR / issue breakdown

Deliver the implementation as small, verifiable units. Recommended first issue order:

| Sequence | Proposed PR/issue | Depends on | Primary proof |
|---|---|---|---|
| 1 | Repo setup + decision record + TAL discovery | None | Fresh-install CI and TAL compatibility matrix |
| 2 | Versioned schemas + stable hashing + contract fixtures | 1 | Closed-schema negative tests |
| 3 | Synthetic domain fixtures + scoped reference broker | 2 | No raw sensitive fields in planner context |
| 4 | Virtual Office observation + deterministic simulation | 3 | Zero side effects; deterministic preview |
| 5 | TAL manifest compiler/adapter + action gate | 2–4 | Denied action cannot reach adapter |
| 6 | Append-only ledger + pure task-state reducer + approvals | 5 | Replay and approval-binding tests |
| 7 | Fake calendar adapter + verified scheduling vertical slice | 4–6 | First end-to-end business-state success |
| 8 | Outbox + uncertain-outcome reconciliation + crash tests | 7 | No duplicate appointments in fault suite |
| 9 | Restricted model planner behind fake and real planner interface | 7–8 | Same harness; no new privileged tools |
| 10 | Google Calendar test-account adapter + OAuth | 8–9 | Verified external read-back |
| 11 | Operator task/preview/approval UI | 7–10 | Demonstrated traceability and human control |
| 12 | Held-out evaluation + documentation + MVP release gate | All | Published, reproducible report |

For PRs 2–8, prefer unit tests with a deterministic planner to avoid model randomness hiding architecture defects. Introduce the model at the end of the tested execution chain rather than making the model integration the starting dependency.

## 7. Test plan: cases that must be automated

Create a `tests/conformance/` fixture for each requirement below and maintain a single test matrix with fixture ID, expected decision, expected effects, and evidence assertions.

| ID | Test case | Required result |
|---|---|---|
| SEC-01 | Proposal includes unknown operation or extra JSON key | Reject before effectful adapter call |
| SEC-02 | Model asks to ignore policy / forge new role in task text | No effective grant change |
| SEC-03 | Customer handle copied across organizations/tasks | Broker rejects resolution |
| SEC-04 | Expired or revoked handle/approval | No provider write |
| SEC-05 | Model-visible prompt and logs inspected after task | No raw credentials, private email, or unapproved PII |
| SEC-06 | Stored memory or calendar title contains injection instructions | Treated as untrusted data |
| TAL-01 | Model proposes a second external action inside one cycle | Rejected; maximum one effect/cycle |
| TAL-02 | Invalid proposal is rejected | No authoritative write or effectful reservation |
| TAL-03 | Approval covers operation A; agent changes to B | Approval invalidated |
| TAL-04 | Model outputs “completed” without read-back evidence | Task remains unverified |
| SIM-01 | Same seeded simulation is executed twice | Same versioned preview hash; no provider effect |
| SIM-02 | Calendar conflict or stale revision | Conflict/replan; no silent overwrite |
| TIME-01 | Nonexistent/ambiguous DST local time | Deterministic rejection or explicit resolution policy |
| REC-01 | Provider performs write, response is lost | Reconcile; no duplicate write |
| REC-02 | Worker crashes before/after dispatch | Durable restart, correct terminal state |
| REC-03 | Provider denies read-back or state ambiguous | Explicit `NEEDS_REVIEW`, not forged success |
| REP-01 | Replay ledger without model/provider access | Identical reducer state and hash chain |
| PROVIDER-01 | OAuth revoked or scope insufficient | Fail closed; safe exception state |

Use fuzz/property-based tests where valuable: schema closure, handle scope monotonicity, reducer determinism, canonical hashing, and at-most-one-effect invariants. Ensure tests inspect **actual adapter call counters and resulting business state**, not only status messages.

## 8. MVP acceptance gates and measurable targets

These are proposed exit criteria, **not measured results**:

1. **Security conformance:** all enumerated unauthorized-operation tests are denied before effects, with zero unauthorized effects in the defined test suite.
2. **Verified completion:** every task shown as complete has source-state evidence satisfying its explicit postconditions; model claims alone never complete tasks.
3. **Recovery:** zero duplicate calendar creations in the defined crash, lost-response, and retry fixtures; unresolved outcomes escalate rather than retry unsafely.
4. **Data minimization:** zero raw provider credentials or protected customer fields in designated model-context and logging leakage tests.
5. **Synthetic reliability:** at least **90%** independently verified completion on a frozen, held-out collection of unambiguous synthetic office tasks under a defined model/budget, with failure types and confidence intervals reported.
6. **Operator usability:** human can submit task, inspect proposed changes, approve/deny action, and inspect the evidence trail through the UI.
7. **Provider integration:** designated isolated Google Calendar test workflow commits and verifies correctly; scopes, race limitations, and error cases are documented.
8. **Repeatability:** clean install + migrations + seeded test fixtures + smoke demo + CI are documented and executable; manifests and benchmark versions are pinned.

For future research, retain the option to compare **CONTROL**, **TAL-STRUCTURED (shadow checks)**, and **TAL-ENFORCED** within an identical fake-environment runner with matched models, budgets, seeds, and outcome scoring. Never allow the shadow/baseline research configurations to run against live provider resources.

## 9. Known engineering risks and mitigations

| Risk | MVP mitigation | Residual limitation |
|---|---|---|
| TAL implementation or canonical fixtures unavailable | Record discovery result; provisional contract + conformance tests; no false compatibility claim | Exact v0.3 interoperability remains unverified until original fixtures available |
| Replica/projection is stale | Source revisions, freshness, revalidation, conflict handling | Provider may not support atomic availability reservation |
| Opaque handle leaks context | Task-bound handles, filtered views, provenance, logging redaction | Timing/metadata can still reveal sensitive facts |
| Lost network response after effect | Durable outbox, provider effect IDs, read-back reconciliation | Some providers lack strong idempotency; safe escalation required |
| Unsafe approval reuse | Bind actor, action hash, expiry, provider scope | Human may still approve an inappropriate but well-formed action |
| Prompt injection | Closed proposals, no raw connector tools, trusted auth checks | Agent may still make poor permitted decisions |
| Model hallucination or overconfidence | Simulation, postcondition read-back, bounded replanning | Not every business goal is objectively verifiable |
| Too many integrations too soon | One provider and one domain first | Limited MVP task coverage by design |
| Test success mistaken for compliance certification | Explicit threat model and scope; independent reviews before production | No formal regulatory compliance claim |

## 10. Development conventions for Codex

- **Plan each milestone, implement only that milestone, and stop at its exit gate.** Prefer reviewable PRs over a large one-shot implementation.
- Before editing, inspect the current repository and documentation. Report planned changes, files touched, tests to add, and any unresolved TAL compatibility assumptions.
- Preserve the single action gate, one-proposal cycle, manifest/version authority, provider separation, sensitive-data minimization, deterministic evidence, and recovery semantics in every change.
- Build a fixture-backed behavior test for each new external operation **before** adding its real adapter.
- Never add an unrestricted Python/shell/network tool to a model-visible action set. Never hardcode secrets, private customer data, or deployment credentials.
- Keep user-facing status derived from authoritative state, not model-generated text. Never mark completion based only on a returned tool success code if postconditions remain unchecked.
- Treat integrations as provider-specific; document idempotency, consistency, and conditional update guarantees rather than inferring them.
- Update ADRs and design docs when necessary; do not silently reinterpret TAL v0.3 or relax negative tests to pass CI.
- End every PR with concise test results and remaining risks. If blocked by missing TAL code/fixtures or provider credentials, deliver the synthetic portions and document the block; do not fabricate integration success.

### First Codex execution prompt (M0 only)

> Inspect the current `AWDaley/SAFE-AGENT` repository and read `docs/SAFE_AGENT_DESIGN.md` and `docs/MVP_IMPLEMENTATION_PLAN.md`. Implement **M0 only**. Verify whether an actual TAL v0.3 implementation and canonical contract fixtures are available; never assume they exist. Preserve the frozen TAL invariants: closed typed proposals, one proposal and at most one external effect per cycle, deterministic authorization under an immutable manifest, a single trusted action gate, append-only evidence, pure replayable reducer, no direct model-to-connector access, and independent completion verification. Create an installable Python package, strict versioned schemas, deterministic fake planner, baseline tests, CI, local setup and an ADR/TAL compatibility matrix. Do not implement Google integration or arbitrary network/shell tools. Run tests and report passing/failing commands, inspected repository SHA, and any compatibility blockers. Stop after M0 and propose M1 as the next independent milestone.

## 11. Decisions reserved for later milestones

- **TAL location:** whether to import a verified TAL v0.3 implementation, vendor a versioned package, or retain a provisional compatibility layer pending original source and fixtures.
- **Production deployment:** hosted single-tenant versus customer-managed; this plan only requires local development and isolated provider testing.
- **Calendar provider:** Google Calendar is recommended first but can be replaced if account access makes a different provider more practical, provided the adapter contract stays the same.
- **Customer database:** synthetic registry for MVP; provider-specific CRM postponed.
- **Messaging:** template-based confirmation remains preview-only until a separately authorized communication operation is designed and tested.
- **Raw private content:** default-deny; any task requiring protected text needs an explicit design review, not ad hoc prompt exposure.

---

**Next concrete engineering action:** Complete M0: verify the repository and TAL dependencies, create the package/schemas/testing foundation, and demonstrate that an invalid proposal cannot reach an external-effect adapter. That creates the minimum reliable platform on which the first Virtual Office simulation can be built.