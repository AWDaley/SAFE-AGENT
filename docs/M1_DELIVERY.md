# M1 delivery and demonstration

M1 implements all six items in the MVP plan's **Synthetic Virtual Office**
section. It is a pure in-memory, fixture-backed prototype, not a production
calendar integration or an authorized task executor.

## Reproduce

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m safe_agent virtual-office-demo
pytest -q
python scripts/export_schemas.py --check
python scripts/export_m1_schemas.py --check
```

The output displays a sanitized versioned calendar projection, the deterministic
M0 planner's `PROPOSED_UNVERIFIED` proposal, and an M1 `CalendarSimulation`
with a diff, conflict codes, source revision, warnings and canonical hash.
The demo prints `SIMULATED_ONLY` and makes no privileged calls. Synthetic
customer names and emails are present solely in the trusted JSON source.

## Interfaces

- `SyntheticOfficeState.from_fixture(path)`: load immutable **private** records
  and validate business relationships.
- `ReferenceBroker.mint(...)/resolve(...)/revoke(...)`: mint opaque handles,
  check task/tenant/actor/operation/kind/expiry/revocation; never expose
  private binding IDs to the planner.
- `VirtualOffice.observe(ObserveCalendar, as_of=...)`: publish `CalendarProjection`
  with safe free slots, time zone, source revision, provenance and freshness.
- `VirtualOffice.simulate(projection, SimulatedChange, actor=...,
  customer_ref=..., now=...)`: return an advisory `CalendarSimulation` with
  deterministic hash, conflict classification, expected postconditions,
  and before/after change description. A conflict or expired projection
  cannot be presented as valid.
- `VirtualOffice.observe_tasks` / `simulate_task`: restricted internal task
  previews. `tasks.create` is not an authorized model action in M1.
- `CalendarProjection.to_planner_projection()`: safe compatibility bridge
  to the M0 `Planner` interface. No TAL or provider write is involved.

## Fixtures

`fixtures/synthetic_office/office_state.json` contains two fictional calendars
and five fictional customers, three templates, task metadata and overlapping
bookings. `dst_cases.json` includes ambiguous/nonexistent New York local wall
times for regression tests. All fixture emails use the reserved `.invalid`
TLD; **do not place real business data in these files**.

## Exit gate

- Given identical inputs and clock, outputs and hashes are identical.
- Preview execution does not mutate provider, booking or internal-task state.
- Projections and previews exclude raw customer names/emails and private
  calendar identifiers.
- Deny unknown, revoked, expired, cross-actor/task/organization handles.
- Reject altered/expired/stale projections, invalid office-hour boundaries,
  conflicting bookings, and ambiguous/nonexistent DST times.
- Original M0 tests and schema bundle continue passing unchanged.

**Deferred to M2/M3:** authoritative TAL manifest/action gate, explicit approval,
immutable evidence ledger, durable broker/projections, calendar-provider state
reconciliation, idempotent commit, and independent verification. Do not merge
these tasks into the M1 sandbox or claim compliance certification.
