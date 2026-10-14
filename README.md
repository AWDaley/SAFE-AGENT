# TERVORO SAFE-AGENT

A contract-first, privacy-minimized foundation for an autonomous office agent.

**Status: M1 complete — synthetic preview only.** SAFE-AGENT now exposes a
versioned, sanitized Virtual Office, a trusted opaque-reference broker, and
calendar/internal-task simulations. There are **no actual calendar or task
writes** and no real provider, credentials or TAL authorization for this domain.

## Architecture and milestones

- [System design](docs/SAFE_AGENT_DESIGN.md)
- [MVP implementation plan](docs/MVP_IMPLEMENTATION_PLAN.md)
- [M1 delivery and reproducibility](docs/M1_DELIVERY.md)
- [M1 architecture/security decision](docs/adr/0002-virtual-office-simulation.md)
- [M0 foundation decision](docs/adr/0001-mvp-scope.md)
- [Upstream TAL compatibility findings](docs/tal-compatibility.md)
- [Security conformance coverage](docs/security-test-matrix.md)

Upstream TAL: [TervoroAgenticLanguage-TAL-](https://github.com/AWDaley/TervoroAgenticLanguage-TAL-)
(commit `23d48c4a4c66ab4d7ef722794f755f0bfcd753d4`). The current published
TAL invoice-domain pack is **not** yet a calendar action-gate integration.

## Local setup

Use Python 3.12+ from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
pytest -q
ruff check src tests scripts
mypy src/safe_agent
python scripts/export_schemas.py --check
python scripts/export_m1_schemas.py --check
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
- M0's TAL boundary **still denies all execution requests** pending the M2
  calendar domain contract and validation tests. No model-to-provider path exists.
- Python module boundaries are not process sandboxing. Trusted components
  require separate identities and enforced egress restrictions before real
  providers or untrusted model code may be connected.

For dev database only: `docker compose up -d postgres` starts a local test
PostgreSQL instance; M1 does not use it. Never apply synthetic credentials or
fixtures to real business systems.

**Next: M2.** Integrate the upstream TAL action gate, immutable manifest
validation, approval binding, durable evidence/replay, and tenant-authenticated
trusted execution path without permitting an unverified effect.
