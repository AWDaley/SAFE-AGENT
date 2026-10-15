# M7-03 — Durable inference budgets and conservative billing holds

**Status:** Opt-in SQL-backed trusted budget control, stacked on PR #26
(restricted HTTPS transport) which depends on PR #25 (inference contracts).
Neither PR #25 nor #26 is merged into main as of this feature's creation.
**M5 LIVE GOOGLE ACCEPTANCE: BLOCKED.**

## Purpose and units

Charge budgets are **integer micro-USD** (1 USD = 1,000,000 micro-USD).
A trusted bootstrap specifies separate global, organization and task limits
plus a per-call maximum. Limits are write-once for an account; restarting
with a different trusted limit fails closed rather than rewriting it.

A proposal's source is still **untrusted**. Inference spending has no
relation to office execution permission. M7-03 does not grant a model any
provider write, reference-resolution, SQL or approval capability.

## Database and conservation rules

Alembic revision 0009_inference_cost_accounts introduces:

- safe_inference_budget_accounts: global, tenant and task spending
  ceilings with durable held and charged micro-USD counters.
- safe_inference_budget_reservations: one immutable request ID and
  task/tenant/route/model/prompt-hash binding, maximum cost, authoritative
  hold, state, time bounds and *unverified* reported token counts.
- safe_inference_budget_events: per-request append-only, hash-linked
  RESERVE / UNCERTAIN / SYNTHETIC_SETTLED / ATTESTED transitions.
- safe_inference_billing_attestations: one HMAC-authenticated, private
  trusted verifier assertion per request (receipt ID, issuer, amount,
  digest of externally obtained evidence, no original invoice payload).

The SQL account update is conditional and atomic:
charged + held + new reservation may NEVER exceed the configured ceiling.
All three scope counters update in one task-scoped SQL transaction or
rollback together. Reservations use a new request ID; replaying a request
is NOT permission to call a model twice. Uncertain charges always keep
the entire worst-case reservation counted against every scope.

PostgreSQL migrations make budget events and accepted billing evidence
immutable using the existing SQL trigger. SQLite create_all is for
synthetic tests and cannot enforce the same production immutability policy.
Hash-linked evidence is independently replayed before returning a
reservation receipt or changing its state.

## Status and recovery

IN_FLIGHT: worst-case cost was reserved durably BEFORE transport dispatch.
The gateway may call a network or mock transport only after successful
admission. A caller crash leaves IN_FLIGHT and the hold persists.

UNCERTAIN: the transport failed, returned an invalid/late response, or
succeeded without independently verified billing. It holds 100% of the
estimate. Provider-reported usage, including reported zero tokens, cannot
free this capacity. A restart recovery scan can mark expired IN_FLIGHT
reservations UNCERTAIN; it can NEVER release them on expiry.

SYNTHETIC_SETTLED: the **exact OfflineMockInferenceTransport** completed a
valid reply and the trusted gateway recorded a zero charge. The reservation
stores an immutable MOCK backend binding; an actual network transport
cannot use the mock-only close path.

RECONCILED: a separately hosted billing verification service provided
one properly scoped HMAC-sealed charge attestation, and the ledger applied
its claimed cost. It releases only the unused hold, moving the attested
charge into each scope's cumulative charged amount. Replaying a receipt,
reusing a receipt for another request, changing scope, and over-reservation
charges are denied. An actual invoice exceeding the reserved limit is an
incident for explicit financial reconciliation, NOT permission to reset
budgets or fabricate a smaller charge.

**Important:** A cryptographic attestation authenticates the issuer; the
code does NOT independently check a hosted provider's invoice. A forged
assertion issued by a compromised verifier can still be misleading. The
verifier's obligation to check an external billing source is not supplied
by this prototype. Do not claim "independently verified spend" solely
on the basis of the HMAC. The public budget DTOs intentionally state
billing verification = false until a future audited verifier exists.

## Trusted usage integration

The M7 InferenceGateway accepts an optional budget_ledger parameter.
When installed, it checks global/tenant/task capacity and pins the
reservation BEFORE any inference backend call. The ledger independently
loads the authoritative task+manifest from SQL, requiring an ASSIGNED
synthetic calendar task, correct organization, allowed operation and
unmodified handles/time window/appointment template.

- Non-mock backends (including mocked versions of the restricted HTTPS
  transport) become UNCERTAIN and keep the full hold even on a successful
  typed proposal.
- A failing/late/malformed backend also becomes UNCERTAIN; it does not
  become a free request and cannot authorize a business effect.
- An exact offline mock may close with a recorded zero synthetic charge.
- A budget service failure is a fail-closed planning error; an existing
  reservation remains in SQL and must be reconciled.
- The legacy M7-01 gateway can still be constructed without a budget
  ledger for synthetic protocol tests. Production bootstrap must explicitly
  attach the budget ledger, authenticated inference sidecar and independent
  spending verifier before any paid inference is admitted. This PR alone
  does not make an unbudgeted M7-02 transport production safe.

Example offline bootstrap:

    from safe_agent.contracts.inference_budget import InferenceBudgetLimits
    from safe_agent.storage.inference_budget import InferenceBudgetLedger
    from safe_agent.inference import InferenceGateway, OfflineMockInferenceTransport

    limits = InferenceBudgetLimits(
        global_microusd=20_000_000,
        tenant_microusd=2_000_000,
        task_microusd=100_000,
        max_call_microusd=20_000,
    )
    ledger = InferenceBudgetLedger(repository, limits)
    gateway = InferenceGateway(
        configured_inference_route, OfflineMockInferenceTransport(),
        budget_ledger=ledger,
    )

No network call, trusted OAuth token or model API key is required by the
demonstration. No billing reconciliation or budget-mutation HTTP route
is exposed to operators or models.

## Acceptance and failure proof

Run:

    pytest -q tests/m7/test_inference_budget.py
    pytest -q tests/m7
    pytest
    mypy src/safe_agent
    ruff check src tests scripts
    alembic upgrade head

CI checks the new accounts/reservations/events/attestation SQL tables in
an isolated test database and runs all existing M5/M6/M7 synthetic and
TAL conformance work, including real no-network Docker smoke.

The M7-03 tests cover strict closed budget schemas; duplicate/replayed
reservations; atomic global/tenant/task ceilings; an unalterable account
limit; crash/restart uncertainty holds; expiry recovery without refunds;
safe, HMAC-attested partial refunds; attestation forgery/replay and
over-limit rejection; event-hash integrity; mock/remote backend separation,
backend failure accounting and continued absence of business effects.

## Outstanding release prerequisites

- Independent external model-provider **billing source verification** and
  a separately managed signing service with audited key rotation.
- Actual token measurement, vendor usage reconciliation and accounting
  for charges that exceed an estimated request ceiling.
- Locked-down runtime service identity, task/tenant authentication and
  inference-only secret custody, with rate limits and hard OS deadline.
- Durable incident notifications, periodic invariant audits, backups
  and high-concurrency PostgreSQL acceptance under contention.
- OS-controlled inference network egress, audited HTTPS sidecar and
  vendor-specific adapter tests.
- Independent security review and branch protection for all authority,
  budget, signature and transport changes.

**M5 live Google release remains BLOCKED.** Budget accounting never
authorizes office writes or independently proves completion.
