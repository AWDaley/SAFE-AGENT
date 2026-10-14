# ADR 0002 — M1 Synthetic Virtual Office and Scoped Reference Broker

**Status:** Accepted for M1 (2026-10-13)

## Context

M0 merged on `main` at `043b11edd9d65f28b1a17e61f61f79bfafc5230e`.
It provides a strict, deny-all planning shell and a pinned `safe_agent.proposal.v1`
contract. M1 must enable useful office simulations without adding a second
policy gate, contacting calendar providers, or changing the M0 wire schemas.

## Decisions

1. **Versioned M1-only contracts.** Keep M0 `Projection` and `Simulation`
   schemas unchanged. `CalendarProjection`/`CalendarSimulation` and
   `TaskListProjection`/`InternalTaskSimulation` have distinct M1 versions and
   an independently generated schema bundle. `CalendarProjection.to_planner_projection()`
   deliberately downgrades to the original safe M0 planner input.
2. **Separate trusted source from model view.** The fixture stores two
   calendars, five customers (one missing email), private bookings, working
   hours, three appointment templates, and two internal tasks. Private
   customer/contact identifiers are resolved only within the trusted service;
   projections expose availability, safe task metadata, scoped handles,
   synthetic source revisions, provenance and freshness.
3. **Opaque, non-bearer references.** Only the trusted `ReferenceBroker` maps
   HMAC-derived random-looking handles to protected record IDs. Every resolve
   rechecks organization, initiating actor, task, operation, resource type,
   expiry and revocation. The broker does not confer task authority; that is
   reserved for TAL's future office-domain gate. Synthetic test keys are never
   reusable for production. The mapping is in-memory in M1.
4. **Read-only calendar simulation.** `observe` uses explicit clocks and
   subtracts busy half-open intervals from calendar-local working hours.
   `simulate` produces advisory diffs, assumptions, conflicts and a canonical
   simulation hash. It does not modify even synthetic bookings. Cache checks,
   source revision/fingerprint checks and expiry reject invalid or stale previews.
5. **DST is fail-closed.** Named IANA zones are required; nonexistent and
   ambiguous local wall times are reported as conflicts. No implicit fold
   selection is allowed in M1. Calendar working-hour boundaries in DST
   transitions likewise fail instead of guessing a local instant.
6. **Internal task extension is preview-only.** `tasks.create` is implemented
   as typed `observe_tasks`/`simulate_task` in the synthetic Virtual Office,
   but it is deliberately **not** added to model-visible M0 `Proposal`, TAL
   capability grants, or any effect dispatcher.
7. **No hidden authority claims.** A successful preview is neither a hold nor
   provider confirmation. M1 uses no external network, OAuth, database writes,
   event ledger, TAL action gate, or approval system.

## Security details and limitations

The observation service requires **trusted caller-supplied identity/time**;
M2 will authenticate that principal, pin the manifest and enforce server-side
clock authority. Python import conventions alone do not provide a security
sandbox: before real model/provider integrations, trusted broker/source code
must run in a separate credentialed process with restricted network access.

Synthetic source revisions are replayable; the simulator also records a
trusted source-content fingerprint to catch unversioned fixture swaps.
Observations are cached only in process, with no long-term persistence.
`ReferenceBroker` handles are non-bearer references, not anonymous data; free
slots and task metadata can still reveal information, requiring tenant and
field-level exposure review before production use.

M2 must integrate the actual TAL `0.3.0` gate and durable event store, reconcile
manifest/proposal/simulation bindings, and deny writes without exact authority.
M3 adds a fake trusted executor and independent completion verification.

## Reproducibility and test strategy

The fixture and clock are explicitly pinned in tests. Hashes use M0's
**provisional** canonicalizer for advisory simulation receipts, not TAL's
production authority signature. CI verifies both M0 and M1 JSON schema
exports, checks Ruff and mypy, and executes negative security tests.

A fully resolved transitive dependency lock remains outstanding; the
`requirements.lock` file freezes direct dependencies only.
