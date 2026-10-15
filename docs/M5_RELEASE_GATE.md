# M5 release decision, TAL pin, and merge instructions

**LIVE ACCEPTANCE: BLOCKED — October 14, 2026.** This document is the
canonical current-state release checklist for the SAFE-AGENT M5 prototype.
It is **not** a claim that a real Google Calendar event has been created,
booked or independently read back by this agent.

## One reviewed TAL source revision

The authoritative SHA lives in [`ci/tal-office-commit.sha`](../ci/tal-office-commit.sha).
The pinned revision is `e9d09ef47c0e0e18f3955f9c58f7a8941baf6c38`,
the reviewed content commit from upstream TAL PR #5, which has since merged
into upstream main. The pin intentionally remains the exact tested source
commit, not a floating upstream branch. Its SHA is not a bearer token.
Both GitHub Actions workflows run `scripts/read_tal_pin.py` to validate a
strict 40-character lowercase commit hash before installing that exact source.
They execute upstream TAL office tests in a job that explicitly installs TAL;
default synthetic-only CI continues to run with no TAL installation.

Upstream TAL's office admission remains non-executable, while a distinct sealed
trusted record is held in SAFE's protected outbox SQL transaction. The TAL
SQLite office journal is not transactionally atomic with SAFE's SQL ledger.
Do not treat its admission event or SHA-256 digest as a cryptographic bearer
permit; reviewers must inspect the actual code and revision, not just the pin.

## Repository integration status (October 14, 2026)

Upstream TAL PR #5 and SAFE-AGENT PRs #13–19 are merged. SAFE-AGENT
PRs #20–24 (trusted task assignment through Docker-isolated offline
planning) were merged into main in order on October 14, 2026.
The merged main head after PR #24 was
`a8a868ca81e8e9cf0d9cff04a4d6255dd7563e17`.
Its SAFE-AGENT and pinned TAL conformance workflows passed.

**Important distinction:** These source merges are NOT independent security
approval, production acceptance or hosted Google acceptance. The
PRs #20–24 had no recorded independent reviews, and main did not have
branch protection enabled at merge time. Establish mandatory reviews,
branch rules and a documented threat-model review before any production
release. Future PRs must retain the unconditional live gate until separate
hosted-provider evidence is independently reviewed.

Do not silently float the TAL source pin. Inspect source revisions and
re-run integrated CI on every authority-changing commit.

## Developer/reviewer commands

```bash
python -m pip install -r requirements.lock
python -m pip install -e '.[dev]'
python scripts/read_tal_pin.py
python scripts/check_m5_release.py --mode offline
# The next command MUST EXIT NONZERO:
python scripts/check_m5_release.py --mode live
```

An offline `CONFIGURED` response means ONLY that checked-in workflows,
required test paths, release-block notices, migrations and TAL pin are wired.
It **does not** mean CI has executed successfully on a given commit. Inspect
the independent GitHub Actions results for Ruff, mypy, pytest, migrations,
TAL office interoperability, provider-backed simulation, fake-provider
outbox recovery and constrained Google MockTransport fault injection.

The live gate currently refuses acceptance **unconditionally**. Local JSON
claims, a reported OAuth handshake, external environment variables, passing
mock HTTP tests, user-provided screenshots or model assertions cannot turn
a release report into a verified Google-hosted effect. A future reviewed PR
must build a trusted verifier of independently collected provider evidence,
with operator-attested identities and a real disposable test calendar, before
considering any change to this gate.

## M5 exit blockers (all mandatory for live acceptance)

- Human-authorized Google OAuth consent with confirmed disposable-account
  calendar ownership, exact allowlist, correct minimal scopes and safe callback.
- An explicitly reviewed live-test-only worker with bounded egress, secrets
  isolation and deterministic TAL/outbox authority binding. The current PR3
  worker enforces MockTransport at construction and use time; it can NEVER
  write to a real Google Calendar.
- One independently verified test-account event creation and readback proving
  exact time, event ID, request/action hashes, tenant, pinned approval,
  durable SAFE evidence and TAL audit status.
- Fault-injection cases against a genuine disposable calendar: already-busy
  slot, 409 collision, expired/revoked approval, OAuth denial, 429, concurrent
  writers, timeout after a possible effect and delayed/missing readback.
  Do not blind-retry an uncertain insert, and do not claim atomic free/busy
  reservation or general exactly-once guarantees.
- Review cross-store atomicity limitations, persist unresolved TAL audit sync
  warnings, verify recovery and alerting on restart, inspect operator identity
  and permission controls, and complete independent security review.
- Publish a sanitized acceptance report with accountable independent reviewer
  and source-of-truth evidence, avoiding actual credentials, raw provider
  calendar identifiers and customer data.

**Current exit judgment:** offline code/CI wiring may be reviewed and merged,
but **M5's real provider acceptance criterion is NOT MET**. Production,
personal calendars, customer accounts, and model-triggered live writes remain
prohibited. See [M5_ACCEPTANCE_READINESS.md](M5_ACCEPTANCE_READINESS.md).
