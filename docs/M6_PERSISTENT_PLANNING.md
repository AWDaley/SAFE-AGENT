# M6-01e — Durable, bounded text-only planning workspace

**Status:** Opt-in local/offline development increment, stacked on PR #22
which depends on PR #21 and PR #20. Do not merge out of dependency order or
treat upstream CI as independent security review.
**M5 LIVE ACCEPTANCE: BLOCKED.** No real Google write paths enabled.

## Objective

Provide durable per-task planning context and repeatable, bounded planning
without elevating an AI model's privileges. The only model-compatible input is
the existing minimized SAFE-AGENT SafeTaskContext and sanitized free/busy
Projection. The planner cannot access broker resolution, the SQL repository,
OAuth credentials, provider clients, arbitrary tools, grants, or approval APIs.

There are two explicit trusted-bootstrap configurations:

- **deterministic**: the existing FakePlanner, with durable planning attempts.
- **text_only**: the existing ClosedJsonPlanner wired to an operator-owned
  Callable that accepts only a sanitized JSON string and returns one closed
  JSON proposal. This transport is injected from trusted service bootstrap;
  the browser never chooses the model, prompt, mode, budget or permissions.

Internal-task creation continues to use its existing deterministic typed
planner path even when the installation has configured text_only for calendar
tasks. No free-form model instructions are used for task creation. No model
callback is installed by default. This milestone does not add a cloud model
subscription, production model runner or process sandbox.

## Durable state model

Alembic revision 0008_planning_workspace adds:

- safe_planning_workspaces: task/tenant/initiator/manifest-bound, mutable
  workspace status; CAS version, bounded attempts and single active lease.
- safe_planning_attempts: immutable metadata for attempt issuance, source task
  version, exact manifest digest, configured mode, and bounded lease expiry.
- safe_planning_outcomes: immutable, hash-sealed outcome type, safe reason code,
  optional accepted proposal digest, and timestamp. It NEVER stores raw model
  completions, model-generated questions, customer names/emails, private
  fixture/provider identifiers, credentials or full observations.

New attempts require exactly the initiating authenticated actor with
task:prepare and task:propose. Each task can consume 1–10 attempts, configured
by the trusted host (default 3), with a 5–300 second planning lease (default
90 seconds). One active attempt per task is protected by the existing
transactional TaskRow lock and version compare. The attempt is durably
recorded **before** calling the model. A stale expected workspace version,
another live attempt, a changed manifest, exhausted budget or non-ASSIGNED
task fails closed without invoking the model.

If the host crashes while planning, its existing lease remains recorded.
After the lease expires, a new authorized request can record the old attempt
as LEASE_EXPIRED and claim a fresh bounded attempt. It cannot re-use the old
attempt token. A model response returned after its lease expiry cannot
authorize a proposal. No provider write can occur during this stage.

A successful trusted simulation/proposal/gate transaction records the
PROPOSED planning outcome in the SAME SQL transaction as the immutable
proposal, approval-needed/eligible state and hash-linked SAFE ledger.
A failure only records a sanitized planning outcome; the authoritative task
stays ASSIGNED. RequestClarification is mapped to safe reason codes such as
INSUFFICIENT_CONTEXT or NO_AVAILABLE_SLOT. Its arbitrary question text is
discarded. Changing the permitted time window requires a newly authorized
task; clarification does not rewrite a manifest or grant new operations.

Replanning can re-observe the **same immutable scope**, but only while the
task remains ASSIGNED and the workspace has remaining attempts. Expired,
tampered and conflicting projections are rejected by the Virtual Office and
policy gate. A successful proposal moves into the existing independent
human-review and synthetic-outbox worker lifecycle.

## Local API

When a trusted host supplies persistent_planning to create_app, two
authenticated routes are enabled:

    GET /v1/tasks/{task_id}/planning

Returns task-scoped, organization-visible status with workspace version,
attempts used/max, safe last_reason and proposal digest. The response contains
no model text or attempt tokens. It requires task:read.

    POST /v1/tasks/{task_id}/plan

Accepts exactly:

    {
      "schema_version": "safe_agent.planning_command.v1",
      "expected_state_version": 0,
      "expected_workspace_version": 0
    }

The response contains only task ID, workspace status, versions, attempt
budget, safe reason code and optional digest; explicitly reports
provider_effect_performed=false and synthetic_only=true. No arbitrary
manifest, instruction, model name, raw output, SQL, URL or new scope is accepted.

The operator UI shows a bounded Plan button and safe attempt status when the
planning service is present. It falls back to the preexisting deterministic
Prepare control when planning is absent. The client never receives a lease
token and does not queue or dispatch effects by planning. Approval and
queue-synthetic remain separate explicit operations with their original roles.

## Trusted bootstrap

Construct the same repository, broker, synthetic VirtualOffice and
TrustedSyntheticWorkflow described in M6_SYNTHETIC_WORKFLOW.md. Opt in:

    from safe_agent.agent.json_planner import ClosedJsonPlanner
    from safe_agent.orchestration.persistent_planning import TrustedPersistentPlanning
    from safe_agent.storage.planning import PlanningWorkspaceStore

    store = PlanningWorkspaceStore(repo, max_attempts=3, lease_seconds=90)
    planner = TrustedPersistentPlanning(
        store, workflow,
        planner=ClosedJsonPlanner(trusted_text_only_generate),
    )
    app = create_app(
        coordinator, signed_operator_tokens,
        enable_operator_ui=True,
        task_provisioner=trusted_task_provisioner,
        synthetic_workflow=workflow,
        persistent_planning=planner,
    )

trusted_text_only_generate must be supplied by the operator's trusted host.
Its transport needs an independently enforced timeout and input/output/token
quota. Python callbacks in the same process are NOT a security sandbox.
Model process/egress isolation, credential separation and production user SSO
are necessary before connecting an untrusted real model to external systems.

For a completely offline demo of the closed text-only interface and durable
workspace (no external model provider or event write):

    python -m safe_agent m6-planning-demo

For verification:

    pytest -q tests/m6/test_persistent_planning.py
    pytest -q tests/m6
    pytest -q
    ruff check src tests scripts
    mypy src/safe_agent

Run Alembic upgrade head only on an isolated developer database. CI validates
the new migration tables, schema/typed tests and the no-live-writes M5 gate.

## Failure and acceptance requirements

Tests demonstrate successful planning with an immutable manifest, budgeted
clarification and retry, rejection of hostile model JSON and extra permissions,
concurrent claim denial, expiry/restart recovery, SQL outcome tamper detection,
independent human approval, durable synthetic queue and verified fake effect
with zero model-side provider calls, and no private source leakage in the
captured model prompt, database outcomes or API responses.

Not included: free-form operator dialogue, live model API configuration,
long-term natural-language memory, autonomous clarification that changes
scope, arbitrary re-planning after effect dispatch, process-level model
sandboxing, trusted worker fleet deployment, durable alerting and Google live
acceptance. A permitted planning proposal is **never** a completed task,
reservation or executable bearer capability.

**Security review required** for database CAS/leases, transaction coupling,
model callback timeouts and domain separation, prompt minimization, safe
exception classification, and the existing TAL admission and worker fences.
