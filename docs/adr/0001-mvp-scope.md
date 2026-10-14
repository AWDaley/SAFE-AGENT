# ADR 0001 — M0 foundation and MVP scope

**Status:** Accepted for M0 (2026-10-13)

## Context and source baselines

At inspection, `AWDaley/SAFE-AGENT` `main` was commit
`c69dd63dcfb45463af3cf8026776cc651036fc52` (README and two specifications).
The independent TAL repository `AWDaley/TervoroAgenticLanguage-TAL-` `main`
was commit `23d48c4a4c66ab4d7ef722794f755f0bfcd753d4`.
No SAFE-AGENT runtime existed in the target repository.

The external TAL repository contains a real closed Pydantic v0.3 harness,
`TALRuntime.apply_proposal`, `InMemoryEventStore`, `SQLiteEventStore`,
`reduce_event`, `canonical_json`, manifest compiler, and offline certification
fixtures. Its released Python distribution metadata currently says
`tervoro-agentic-language==0.4.0rc2`, while internal `TAL_VERSION` remains `0.3.0`.
Its domain pack supports **invoice resolution**, not office calendar actions.

## Decision

- Implement **M0 only** in a Python 3.12+ modular package (`src/safe_agent`).
- Keep planner and typed Virtual Office *contracts* unprivileged and synthetic.
- Accept one discriminated, closed-schema proposal per planning cycle.
- Do not grant rights based on prompts, proposal contents, or opaque IDs.
- Use a deny-all TAL interface until a **version-pinned office domain pack** and
  real compatibility fixtures exist. Avoid a second local authorization engine.
- Adopt the documented TAL `tal-json-c14n-0.2` canonicalization shape only for
  local **non-authoritative artifact IDs**, explicitly marked provisional.
- Freeze the MVP vertical slice: create a 30-minute synthetic customer follow-up
  within a permitted business window; Google Calendar adapter and completion
  verification belong to later milestones.
- No production secrets, customer PII, or unrestricted network/shell/SQL tools.
- Use Python Pydantic v2 strict models and pytest. Keep source in one repository;
  later enforce worker/runtime process separation before real integration.

## MVP safety constraints

- **One** typed proposal and at most **one** external effect per TAL cycle.
- Synthetic preview is not permission or completed work.
- Source data must be rechecked by the trusted adapter at commit time.
- A task may be marked `COMPLETED` only through independent verified evidence.
- `OUTCOME_UNKNOWN` cannot be optimistically retried with duplicate effects.
- Model-visible context contains scoped handles and free/busy slots only.

## Explicitly deferred

Database migrations, real provider integration, durable outbox and event ledger,
reference broker, Virtual Office simulator, API authentication, human approvals,
real model inference, front-end UI, and TAL office-domain extension are **M1+**.
Do not infer functionality from a Pydantic schema or mocked planner.

## Reproducibility limitations

`requirements.lock` pins direct packages, **not the transitive dependency graph**.
The build environment used for M0 has no package-index access and no cached Ruff
or mypy installations. `uv lock --offline` could not resolve Pydantic from its
cache. CI installs known direct pins and runs Ruff and mypy, but fully locked
hashes/`uv.lock` are a release gate for subsequent milestones. A human or Codex
session with package index access should generate and review `uv.lock` rather than
asserting the direct-pins file is a complete lock.

## Consequences

This commit is a tested planning foundation, **not** a production-safe agent,
calendar integration, TAL v0.3 conformance certificate, or complete MVP. M1
adds synthetic Virtual Office behavior while TAL domain integration remains a
separately verified boundary.
