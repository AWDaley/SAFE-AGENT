# SAFE-AGENT ↔ TAL compatibility matrix (M0)

**TAL source inspected:** https://github.com/AWDaley/TervoroAgenticLanguage-TAL-
at `23d48c4a4c66ab4d7ef722794f755f0bfcd753d4`.

This report records observed source, not inferred behavior from a design document.
TAL **does exist** as a separate public repository. This is **not** a claim that
calendar-specific TAL authority has been implemented.

| Concern | Verified upstream implementation | SAFE-AGENT M0 | Compatibility |
| --- | --- | --- | --- |
| Closed strict models | `src/tal/core.py` `StrictModel` (`frozen`, `strict`, `extra=forbid`) | Strict versioned schemas | Similar contract; independently tested |
| Proposal parser | `src/tal/adapters.py` `parse_proposal_json` (`Proposal` discriminator `kind`) | `safe_agent.contracts.actions` (`operation` discriminator) | **Different envelopes**; office translator needed |
| Manifest compiler/hash | `src/tal/compiler.py` `compile_manifest`, `verify_manifest_hash` | Untrusted-side `TaskManifest` type only | No manifest compiler integration |
| Action gate | `src/tal/runtime.py` `TALRuntime.apply_proposal` → `_action_gate` | `UnconfiguredTALAuthority` denies | **NOT integrated** |
| Domain operations | `src/tal/invoice.py` `ToolId`, invoice pack | `calendar.create_event` proposal | **Unsupported upstream** |
| Canonicalization | `src/tal/core.py` `CANONICALIZATION_ID = tal-json-c14n-0.2`, `canonical_json`, `sha256` | Provisional non-authoritative local serializer | Hash semantics must be conformance-tested against pinned TAL |
| Hash-chained events | `src/tal/events.py` `seal_event`, `reduce_event` | Event schema type only | **NOT integrated** |
| Persistence | `src/tal/storage.py` in-memory + SQLite stores | No persistent event store | **NOT integrated** |
| Replay | `src/tal/runtime.py` `replay` | Not implemented | **NOT integrated** |
| Verification | Invoice-domain validators and completion gate in TAL runtime | Receipts/evidence type only | Calendar evidence contract needed |
| Multi-agent topology | v0.3 coordination models in `src/tal/coordination.py` | Single agent only | Outside MVP |
| Package metadata | `pyproject.toml` `version=0.4.0rc2`, internal `TAL_VERSION=0.3.0` | No hard TAL install dependency | Pin revision / check version before integration |

## Required integration actions before enabling ANY real write

1. Agree an office domain pack in the TAL project with exact `calendar.create_event`
   capability, argument validation, tenant scope, budgets, approval and completion rules.
2. Pin an approved upstream commit and license/dependency distribution strategy.
3. Implement translation between SAFE-AGENT's operation envelope and TAL's
   `ProposalKind`/`ToolProposal`, without model authority over target IDs.
4. Port canonical manifest fixtures and verify digest byte-for-byte against the
   **imported TAL** implementation; test Unicode, time zone, and missing fields.
5. Wire TAL's genuine `apply_proposal` gate to a trusted calendar adapter; the
   model worker must never receive adapter handles or provider credentials.
6. Test side-effect-free rejection, one effect/cycle, replay, and independent
   calendar postcondition read-back before enabling a real provider.

## Runnable checks

- `pytest`: the M0 suite exercises strict typed boundary rules **offline**.
- `SAFE_AGENT_REQUIRE_UPSTREAM_TAL=1 pytest tests/conformance/test_upstream_gate.py`:
  an **optional explicit** upstream-import smoke check; requires separately
  installed pinned TAL source. It is intentionally skipped by standard CI.
- Passing that import check is **not** certification of the office-domain gate.

**Result:** verified TAL source code exists; calendar-domain compatibility is
**blocked/unimplemented**. The M0 planner is intentionally non-effectful.


## M2 update — provisional office-domain policy (2026-10-13)

M2 adds `safe_agent.tal.office_gate.OfficeGate` and deterministic
`compile_office_grant`, but they operate **only** on synthetic workflows.
The upstream TAL v0.3 invoice-only `TALRuntime.apply_proposal` has not been
extended, imported, or called for calendar operations. Its `ToolProposal`
discriminator, invoice tool enum and event schema remain incompatible with
the SAFE-AGENT office domain. The M2 `ACTION_ELIGIBLE` event is not an upstream
TAL permit. `UnconfiguredTALAuthority` still denies all real writes.

M2 introduces a separate provisional office ledger whose events replay
independently. It does not reuse or replace upstream TAL's invoice ledger.
Before enabling any provider writes: implement TAL calendar domain pack and
upstream conformance fixtures; ensure the single external authorization gate
uses upstream TAL and verifies current business/provider state; do not rely on
M2's synthetic policy result as a privileged execution token.
