# M7-03 — Durable, conservative inference spending and usage ledger

**Status:** Opt-in SQL accounting layer. Stacked on M7-02 PR #26 and
M7-01 PR #25, both awaiting independent security review.
**M5 LIVE GOOGLE ACCEPTANCE: BLOCKED.** No real provider inference credentials,
billing integration, personal customer data or Google event writes are enabled.

## Objective

Make model inference spending an explicit, bounded, SQL-committed operation
rather than a calculation in application memory. Before calling a potentially
paid model, the trusted harness must reserve the maximum price estimate for
that request under all of its global, tenant, and task spending ceilings.
A timeout, crash, malformed response, unavailable backend, or unverified
usage report can never restore its reserved capacity or be billed as zero.

The existing task-scoped PlanningWorkspaceStore still controls reasoning
attempts and policy/approval. InferenceBudgetLedger controls monetary
reservations and in-flight calls. They complement one another: neither can
grant execution, change a manifest, or issue provider credentials.

## Data model

Migration 0009_inference_budget adds:

- safe_inference_budget_scopes: global / organization / task scopes with
  immutable limit/profile hash, maximum and held micro-USD, call count,
  in-flight count and manual-review freeze status.
- safe_inference_budget_reservations: one unique, immutable request binding
  to task, tenant, prompt digest, route/model revision, price policy hash,
  pricing coefficients, upper token-proxy bytes, maximum financial hold,
  bounded reported usage and terminal state. No prompts, model output,
  customer names, account passwords, calendar IDs or API keys.
- safe_inference_budget_events: append-only, SHA-256-linked RESERVED and
  terminal events with privacy-minimized metadata. PostgreSQL has an
  immutable trigger for these audit records. The trusted read verifies
  each event digest, order, chain and current reservation fields.

Costs are integer micro-USD with SQL BIGINT columns where necessary,
avoiding float rounding and overflow at tenant/global scales.
Provider-reported token counts and reported cost remain explicitly
UNVERIFIED. A receipt never asserts an independently verified bill.

## One-call state machine

The trusted host creates InferenceBudgetLimits and explicitly initializes
the global budget using ledger.configure(now=...). No public API may
configure or reset those limits. Different profile hashes, route changes,
or mismatched stored budget constraints fail closed. Increasing limits or
resetting counters must use a separately reviewed admin migration.

The gateway validates canonical sanitized context before charging. It
creates a fresh inference request, then calls ledger.reserve(request,
pricing, now=...) **BEFORE** running any inference transport. Admission
requires a registered task in the correct organization, an assigned
task state, an unexpired window, a valid price policy and remaining
capacity in all three budget scopes. Every reservation increments the
lifetime call count, financial hold and active count in one transaction.

For PostgreSQL, the global budget row is SELECT FOR UPDATE and serves as
the serialization lane across all tenants before tenant/task locks.
SQLite isolated tests use BEGIN IMMEDIATE before any read to prevent
lost updates between writers. An unavailable database always refuses
dispatch; no unlocked fallback exists.

Terminals:

- RESERVED: possible dispatch, maximum estimated charge already held.
- RECORDED_UNVERIFIED: accepted typed proposal and provider-reported usage
  recorded with a verified-as-false receipt. The full maximum hold remains
  permanently charged against available budgets.
- UNCERTAIN: timeout, loss, invalid response or expired/resumed request.
  The full charge remains held even after freeing its concurrency slot.
- REPORTED_USAGE_OVERRUN: terminal UNCERTAIN plus a persisted manual-review
  freeze of all three scopes. No new calls can bypass a suspected charge
  exceeding the configured cost/token proxies.

No refund, balance reset or proof-of-provider-bill API exists in M7-03.
A future independent billing reconciler must verify exact provider
invoices and implement a reviewed credit/refund operation without
allowing an inference transport to sign its own evidence.

A network call may complete but crash before recording its usage. The
database still retains RESERVED until recovery marks it UNCERTAIN.
A late reply cannot turn that uncertain request into verified success.
The same request ID can never be reserved twice, and a duplicated
terminal call can only repeat exactly the previously recorded details.

## Trusted bootstrap (offline mock)

    from safe_agent.contracts.inference import InferenceRoutePolicy
    from safe_agent.contracts.inference_budget import InferenceBudgetLimits
    from safe_agent.inference import InferenceGateway, OfflineMockInferenceTransport
    from safe_agent.storage.inference_budget import InferenceBudgetLedger

    limits = InferenceBudgetLimits(
        profile_id="offline_budget_v1",
        global_max_microusd=0, tenant_max_microusd=0,
        task_max_microusd=0,
        global_max_calls=100, tenant_max_calls=50,
        task_max_calls=3,
    )
    ledger = InferenceBudgetLedger(repository, limits)
    ledger.configure(now=trusted_now)
    gateway = InferenceGateway(
        InferenceRoutePolicy(
            route_id="offline_mock", model_revision="mock_calendar_v1",
        ),
        OfflineMockInferenceTransport(),
        clock=trusted_clock,
        budget=ledger,
    )

The zero-priced offline profile still applies call-count and concurrency
limits, so a free mock cannot execute unlimited planning calls. A trusted
paid-price profile must explicitly specify per-million-token rates and
nonzero charge caps in both InferenceRoutePolicy and InferenceBudgetLimits.
Do not connect a paid provider with default zero-priced mock assumptions.

No model can call reserve, finish, uncertain, recover, scope_view or
configure through the operator API. The ledger is supplied exclusively
from trusted server bootstrap.

## Validation / failure injection

tests/m7/test_inference_budget.py covers:

- Missing configuration, mismatched profile and stale/falsified task scope.
- Durable pre-call reservation, all-scope accounting and no duplicate IDs.
- Global, tenant and task spending/call/in-flight ceilings.
- SQL concurrency with independent writers, cost ceiling and no model
  dispatch on denied reservation.
- Typed success storing unverified usage and no financial refund.
- Partial response, malformed output, backend crash and late completion
  holding full estimated charge with no duplicate effects.
- Recovery after SQL process restart: expired in-flight work becomes
  UNCERTAIN and concurrency is released, not monetary budget.
- Exceeding reported token/cost ceiling freezes further inference until
  operator security review, without pretending excess billed cost is known.
- Idempotent exact terminal replay, immutable event tamper detection
  and non-exposure of provider output, IDs or credentials in SQL records.

Full CI runs Ruff, mypy, full pytest, M7 focused tests, offline Docker,
M5 live release prohibition, pinned TAL conformance and the Alembic
migration head on a disposable SQLite file. Passing CI is not independent
billing verification or authorization for live provider execution.

## Outstanding production gates

The M7-02 egress service itself must have independently enforced
wall-clock/DNS/slow-trickle deadlines and dedicated, authenticated
credentials. A hosted provider adapter must return verified account and
billable usage records independent of an LLM response. Real billing
reconciliation requires careful exchange rates, token/price schedules,
billing cycle controls, settlement/idempotency and charge dispute recovery.
Operator-managed independent reconciliation and a freeze-clear workflow
must be reviewed before refunds or spending increases become possible.

GitHub main remains unprotected; PR #25 and PR #26 must receive independent
review before their stacked successors can merge. No actual LLM API and
no live Google write is enabled here.

**Models propose; the deterministic harness controls permission,
execution, evidence, budgets and billing.**
