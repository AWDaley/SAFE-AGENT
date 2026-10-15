# M6-01c — Deterministic synthetic workflow preparation and outbox

**Status:** Implemented on stacked PR #21 (depends on M6-01b PR #20).
**M5 LIVE ACCEPTANCE: BLOCKED**. No Google or production effects enabled.

## Goal and boundaries

An authenticated local operator can create an authorized synthetic task with
the opt-in M6-01b provisioner, prepare a deterministic Virtual Office proposal,
review the stored simulation in the existing inbox, obtain a separate human
approval if its manifest requires it, and **queue a synthetic-only durable
outbox intent**. Only a separately configured trusted
`SyntheticRecoveryWorker` may dispatch and independently verify the fake
effect. The HTTP route never invokes `process_one` or a provider write.

This increment uses the built-in deterministic FakePlanner only. It does not
create an LLM tool, accept model-generated authority, implement arbitrary
natural-language tasking, grant credential access, or enable a live calendar.

## Backend configuration

In trusted local bootstrap code, create all M6-01b dependencies using the
**same** SQL repository, reference broker, synthetic office source and
coordinator. Do not use production calendars or credentials.

For an optional durable synthetic worker, use the existing fake provider:

```python
from safe_agent.adapters.fake_calendar import FakeCalendarAdapter
from safe_agent.adapters.fake_tasks import FakeInternalTaskAdapter
from safe_agent.execution.synthetic import SyntheticExecutionGateway
from safe_agent.execution.recovery import SyntheticRecoveryWorker
from safe_agent.orchestration.synthetic_workflow import TrustedSyntheticWorkflow
from safe_agent.storage.provider import DurableFakeProvider
from safe_agent.tal.office_gate import OfficeGate
from safe_agent.api import create_app

provider = DurableFakeProvider(repo.engine, synthetic_source)
calendar = FakeCalendarAdapter(synthetic_source, durable_store=provider)
tasks = FakeInternalTaskAdapter(synthetic_source, durable_store=provider)
gateway = SyntheticExecutionGateway(
    repo, office, OfficeGate(broker), broker, calendar, task_adapter=tasks,
)
workflow = TrustedSyntheticWorkflow(
    repo, office, broker, coordinator, gateway=gateway,
)
app = create_app(
    coordinator, signed_operator_tokens, enable_operator_ui=True,
    task_provisioner=trusted_task_provisioner, synthetic_workflow=workflow,
)
worker = SyntheticRecoveryWorker(
    repo, broker, calendar, task_adapter=tasks, office=office,
)
```

The worker must be hosted as a trusted harness component, not a browser tool.
Use the existing supervised worker lifecycle to invoke
`worker.process_one(now=...)` from an appropriately controlled process.
For a local test, explicitly invoke `process_one(task_id=..., now=...)`
after queueing. The M6 operator interface can refresh task/execution status
but cannot dispatch or claim effects.

Never wire a Google client into this synthetic gateway/worker; the existing
type fences require the exact fake adapter classes.

## HTTP contracts

All endpoints are **opt-in** through `synthetic_workflow` and require the
existing externally minted local operator bearer token.

`POST /v1/tasks/{task_id}/prepare` requires an initiator with both
`task:prepare` and `task:propose`. The body is:

```json
{"schema_version":"safe_agent.synthetic_workflow_command.v1",
 "expected_state_version":0}
```

The service loads the pinned immutable manifest, checks exact actor/tenant and
ledger state, restores the private broker bindings only after HMAC validation,
observes a permission-scoped Virtual Office projection and selects one
deterministic calendar opening or internal-task due date. It recomputes an
advisory simulation and records the preview, proposal, TAL-style local policy
decision and hash-linked ledger events **atomically in one SQL transaction**.
Preparation works only for a freshly assigned task. A conflicting, stale,
expired or unauthorized preparation fails closed; repeating it does not add a
second proposal cycle. The response contains the sanitized task ID, phase,
version and proposal digest, with `provider_effect_performed=false`.

`POST /v1/tasks/{task_id}/queue-synthetic` requires the **initiating actor**
with `task:execute`, a current `READY_TO_COMMIT` snapshot and the exact
state version. If approval is required, the independent reviewer must have
already approved the exact action hash before queueing. The trusted
`SyntheticExecutionGateway.enqueue` revalidates the stored proposal,
simulation, manifest, broker scope, source state, deadline, budgets and
approval before writing a durable synthetic-only outbox intent. The response
is an `EXECUTING` queue receipt with `queued_only=true`; it is **not**
completion evidence. Incompatible or duplicate requests are denied.

The already-existing `GET /v1/tasks/{task_id}`,
`/preview`, `/execution` and `/events` provide evidence-backed status
without exposing private customer/provider IDs.

## Verifier and recovery

The separately hosted synthetic worker claims only `provider_kind=synthetic`
and uses a durable fake-provider idempotency key. Independent read-back
determines `COMPLETED`; lost responses can remain `OUTCOME_UNKNOWN` and be
reconciled, while unresolved outcomes become `NEEDS_REVIEW`. The model and
the browser never set `COMPLETED`. The optional upstream TAL interoperability
and audit checks remain separately constrained by the existing deployment
configuration; the local provisional `OfficeGate` is **not** live provider
authorization.

## Validation and review gates

Run `pytest -q tests/m6`, the full `pytest`, `ruff check src tests scripts`,
`mypy src/safe_agent`, browser syntax and Alembic migration smoke.
`tests/m6/test_synthetic_workflow.py` exercises both workflows, separation
of duties, exact hash-bound approvals, stale versions, tampering of private
broker records, policy denial rollback, outbox-only HTTP, independent fake
read-back and lost-response recovery without duplicate booking.

**Unresolved for production:** Human identity/SSO, isolated execution
process and egress policy, durable worker supervision, secret rotation,
operator UX evaluation, held-out reliability benchmarks and real hosted
Google test acceptance. PR #21 must not merge before independent review of
the new atomic authority boundary, API role checks and worker wiring.
