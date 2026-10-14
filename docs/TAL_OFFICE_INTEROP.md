# TAL office calendar admission interoperability — staged integration


> **Historical implementation snapshot.** This note records an earlier
> stage and its then-current upstream SHA; some earlier "not implemented" text
> is superseded. As of PR5, the only currently pinned TAL source is
> [`ci/tal-office-commit.sha`](../ci/tal-office-commit.sha), with experimental
> trusted office authorization and mock-only Google execution. **M5 LIVE
> ACCEPTANCE: BLOCKED.** See [M5_RELEASE_GATE.md](M5_RELEASE_GATE.md).

**Status:** offline admission interoperability only; **real Google Calendar writes remain disabled**.

**Pinned upstream:** `AWDaley/TervoroAgenticLanguage-TAL-` commit
`a39de505fdd0e676b239d87525eb53ce3a36dc8c`.

The upstream `tal.office` module adds typed calendar policy, proposal, evidence, and
approval contracts, canonical grant hashes, and a pure model-free admission evaluator.
Its `OfficeAdmissionDecision.authorizes_execution` is **always false**.

`safe_agent.tal.upstream_office.UpstreamOfficePolicyGate` is an **opt-in**,
fail-closed adapter used only from the existing trusted coordinator and synthetic
execution pathway. It runs SAFE-AGENT's current (provisional) gate first, then
requires a matching TAL office-domain decision before accepting the proposed
calendar effect. This **cannot** grant an operation denied by SAFE-AGENT's original
gate. The adapter first checks byte-for-byte SHA-256 compatibility for the SAFE
manifest and proposal; it constructs the TAL grant, proposal and evidence from
trusted immutable records rather than model-supplied instructions or arbitrary
URLs. Import errors, canonicalization differences, mismatched TAL status, or
TAL denial all fail closed. Existing internal-task admission stays synthetic-only.

The upstream TAL office module is currently a *domain-admission experiment*;
it **does not extend** the invoice-only `TALRuntime.apply_proposal`, integrate
with the TAL immutable event store, issue cryptographic execution permits, or
certify real provider authorization. SAFE-AGENT's M4 durable outbox does not
accept Google adapter instances, and the M5 Google service continues to reject
model-triggered provider writes. There is no environment flag that bypasses
this restriction. These properties are release blockers for real writes.

## Local tests

```bash
python -m pip install -r requirements.lock
python -m pip install -e '.[dev]'
python -m pip install 'git+https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git@a39de505fdd0e676b239d87525eb53ce3a36dc8c'
pytest -q tests/m5/test_upstream_office_bridge.py
```

The GitHub Actions `tal-office-interop` job pins and imports the upstream commit.
Without the optional TAL package, the existing synthetic M0–M4 workflow remains
functional and the opt-in interop test is skipped in the default job. The
`UpstreamOfficePolicyGate` itself **never** grants authorization if the package
is missing.

## Remaining gates before a real Google write

1. Generalize upstream TAL's single `TALRuntime.apply_proposal` and event ledger
   for office-domain actions, including definitive version and audit bindings.
2. Route the existing M4 leased outbox through that upstream gate before every
   dispatch (including retries); maintain exactly one trusted execution pathway.
3. Integrate the Google test adapter as a separately reviewed trusted worker
   restricted to an isolated calendar; enforce opaque IDs and OAuth allowlists.
4. Test timeout-after-commit, provider readback delays, deterministic-ID 409,
   simultaneous writers, stale projection, 403/429 and revoke behavior against
   a **user-authorized disposable Google test calendar**. Do not assert Google
   provides atomic slot reservation or guaranteed exactly-once creation.
5. Complete process isolation, secret management, and security review before
   considering any business-data or production use.
