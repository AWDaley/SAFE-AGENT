# M6 foundation — local operator inbox and approval review

**Status:** Offline/local operator workspace, integrated into main; M6-01b adds optional synthetic task intake.
M5 LIVE ACCEPTANCE remains **BLOCKED**. This UI cannot dispatch a Google write.

## Features

- Opt-in, same-origin UI at /operator, with CSP, no CDN, no inline JavaScript
  and no cross-site networking or runtime script injection.
- A trusted, server-minted **local test bearer token** is entered only into
  the page memory. It is never stored in cookies, localStorage, sessionStorage,
  URL query strings, audit logs, or rendered page content. Disconnect clears it.
- GET /v1/tasks: organization-scoped, task:read-protected, bounded, stable
  lexicographic task-ID pagination. No private provider IDs or customer data.
- GET /v1/tasks/{task_id}/preview: reconstructs the already persisted,
  hash-verified synthetic preview for calendar or internal tasks. A proposal
  preview remains advisory; no slot is reserved and it is NOT a Google-originated
  availability statement.
- Existing task snapshot, execution summary and audit timeline endpoints are
  reused. The UI renders **only textContent** of any API-derived value.
- Existing POST approval/denial retains server-side separation of duties,
  proposal digest, expected state version, deadlines and expiry. UI requires
  explicit preview review checkbox, operator-selected expiration and a
  confirmation prompt. Neither decision creates an event.

## Trusted task assignment (M6-01b)

When the host explicitly supplies a TrustedTaskProvisioner, an operator with
task:create may view only workflow options preconfigured for their tenant and
actor, then create a strictly bounded synthetic task through the assignment
form. The trusted backend creates the manifest, scoped references and
hash-linked registration atomically; the form has no raw resource ID,
permission editor, arbitrary manifest field or model/provider action.
The new task is ASSIGNED only: planning and execution do not begin on submit.
A repeated idempotency key can return only the original exact task.

See M6_TASK_INTAKE.md for protected persistence, setup, tests and limitations.

## Deterministic synthetic preparation (M6-01c)

When a trusted operator additionally configures TrustedSyntheticWorkflow,
a task initiator with task:prepare and task:propose may prepare one assigned
synthetic task. A fresh Virtual Office observation and simulation are checked,
then preview, proposal and local policy decision are persisted together.
If approval is required, the independent reviewer uses the existing approval
pane. The initiating actor with task:execute may **queue** the permitted
action; only an independently hosted trusted synthetic recovery worker performs
the fake effect and verifies read-back. No browser route performs provider
dispatch or allows Google writes.

See M6_SYNTHETIC_WORKFLOW.md for bootstrap, role checks, recovery, safety and
acceptance coverage.

## Supervised fake-provider effects (M6-01d)

The worker host may opt into SupervisedSyntheticWorker to consume already
queued, synthetic-only intents. The interface still provides only the
existing queue action and read-only status. The supervisor uses bounded SQL
claims, interruption-aware pauses and fail-closed error thresholds; provider
readback and NEEDS_REVIEW decisions remain with the trusted recovery worker.
No operator HTML/JavaScript gains a worker execution route.

See M6_SUPERVISED_SYNTHETIC_WORKER.md.

## Setup

Install API dependencies and create the service from trusted harness objects:

\`\`\`python
from safe_agent.api import create_app
app = create_app(coordinator, signer, enable_operator_ui=True)
\`\`\`

Serve on trusted localhost using existing API deployment instructions. Open
/operator and paste a locally issued operator test token. A real public
deployment must replace local HMAC tokens with SSO/OIDC, enforce HTTPS, add
CSRF/session protections when using cookies, security hardening, permissions,
audit review, and reliable credentials. **Do not paste production credentials.**

The UI is deliberately optional; create_app defaults to
enable_operator_ui=False. Trusted synthetic task intake is also optional and
must be enabled separately through task_provisioner; without it the page
remains review-only. It has no identity onboarding, free-form model chat,
token issuer, direct execution control or broad connection-management surface.
Task creation is privileged and authenticated, never an unrestricted API.

## Safety and required checks

Run pytest tests/m6, the default synthetic and TAL conformance suites,
Ruff, mypy, the offline M5 release gate and migration smoke.

The UI does not call GoogleTestOutboxStager, GoogleTestCalendarHTTP,
OfflineGoogleTestRecoveryWorker, or any provider write endpoint.

M6-01 remains **partial**: the inbox, stored previews, explicit decisions,
safe timeline, opt-in task assignment, and deterministic synthetic
preparation/outbox queue are implemented. The trusted worker remains an
independent process responsibility. Natural-language model planning,
connection UI, evaluation, packaging and production identity remain future
work. M5 independently remains blocked.
