# M0–M2 security test matrix

Statuses describe **implemented synthetic tests**, not guarantees about an unbuilt
live system. Later conformance suites must inspect actual adapter effect counters.

| ID | Check | M0 status | Evidence |
| --- | --- | --- | --- |
| SEC-01 | Reject unknown operation and unknown JSON fields | PASS locally | `test_malformed_or_privileged_proposal_denied` |
| SEC-02 | Prompt text cannot confer an operation grant | Structural only | Closed operation vocabulary; authorization deferred |
| SEC-03 | Reject cross-task/organization planning inputs | PASS locally | `test_runner_rejects_cross_*` |
| SEC-04 | Revoked/expired/cross-actor/task/tenant handle cannot be resolved | PASS M1, synthetic only | `test_handle_is_opaque_and_scope_bound`, `test_expired_revoked_and_unknown_handles` |
| SEC-05 | Model context excludes raw email/credentials | PASS on synthetic fixture | `test_planner_context_contains_only_scoped_metadata` |
| TAL-01 | Max one effect per cycle | Schema enforces budget=1 | Actual effect-count test deferred M2 |
| TAL-02 | Invalid proposal has zero external effects | No external writer exists | Action gate integration deferred M2 |
| TAL-03 | Approval cannot transfer to other action | Contract includes proposal hash | Runtime approval policy deferred M2 |
| TAL-04 | Model cannot mark task completed | PASS locally | TaskRunner always returns `PROPOSED_UNVERIFIED` |
| SIM-01 | Deterministic synthetic planning | PASS locally | `test_fake_planner_is_deterministic...` |
| SIM-02 | Stale/altered projection rejected for simulation | PASS M1, synthetic only | `test_expired_projection_denied`, `test_changed_source_revision_denied`, `test_changed_projection_fields_rejected` |
| REP-01 | Persisted event chain replays identically | Not implemented | Upstream TAL does support replay |
| REC-01 | No duplicate effects after unknown outcome | Not implemented | Outbox/reconciliation deferred M4 |
| SIM-03 | Simulation is deterministic and has zero source effects | PASS M1, synthetic only | `test_deterministic_projection_and_simulation`, `test_overlapping_booking_is_a_conflict_without_changes`, `test_internal_task_creation_is_preview_only` |
| SIM-04 | DST fold/gap and office-hours conflicts reported | PASS M1, synthetic only | `test_dst_conflict_in_full_calendar_simulation`, `test_restricted_times_have_explicit_conflict` |
| PRIV-01 | Projection/preview serialization excludes synthetic raw customer info | PASS M1 on fixtures | `test_no_private_customer_fields_in_projection_or_preview` |
| UPSTREAM-01 | Pinned TAL import matches expected IDs | Optional smoke gate only | Import check skipped without TAL install |

Unimplemented checks **must not** be marked PASS or used to claim enterprise
information-security or compliance certification.


## M2 evaluated controls (synthetic environment only)

| ID | M2 result | Evidence |
| --- | --- | --- |
| TAL-01 | PASS synthetic; one accepted proposal/cycle and no effect dispatcher | `tests/m2/test_gate_and_ledger.py` |
| TAL-02 | PASS synthetic; denied proposals append audit events only | `tests/m2/test_security_boundaries.py` |
| TAL-03 | PASS; exact proposal hash, manifest, approver identity and expiry checked | `tests/m2/test_planner_bridge.py` and approval tests |
| REP-01 | PASS synthetic; hash-chain replay and persisted projection comparison | `test_ledger_tampering_and_deleted_events_detected` |
| SEC-04 | PASS synthetic; broker HMAC export/restore and revoked reference rejection | `test_signed_broker_bindings_survive_restart_and_reject_tampering` |
| API-01 | PASS local; auth bearer token and tenant/role isolation | `test_operator_api_prevents_cross_tenant_access_and_self_approval` |
| DB-01 | PASS isolated PostgreSQL CI; migration and immutability triggers | `scripts/pg_smoke.py` (requires hosted CI) |
| TAL-UPSTREAM-CALENDAR | **BLOCKED**; not calendar-domain TAL v0.3 conformance | `docs/tal-compatibility.md` |
| M3-DISPATCH | **NOT IMPLEMENTED**; zero real effects possible | No provider adapters or dispatch method |

These results do not certify production data security or real-world compliance.

## PR5 M5 office/Google test conformance snapshot (October 14, 2026)

The table above records **historical M0–M2** checkpoints. The stacked M5 PRs
add further tested controls, but no Google account or production workflow is
approved and the original entries must not be read as current release status.

| Control | Offline evidence | Live acceptance |
| --- | --- | --- |
| TAL authorization | Strict office authorization record, SQL/action binding and cross-repo tests | **BLOCKED** |
| Google test connection | OAuth PKCE, encrypted vault, allowlist/tenant tests using mock HTTP | **BLOCKED** |
| Durable Google outbox | Single intent, CAS/lease, synthetic-worker isolation, provider-sourced advisory simulation | **BLOCKED** |
| Uncertain Google write | Mock-only deterministic ID, readback and no blind retry; needs-review escalation | **BLOCKED** |
| Provider-source freshness | Exact observation/proposal pin, expiry and pre-insert free/busy; NOT an atomic slot lock | **BLOCKED** |
| Real account consent, ownership and revocation | Code paths and error simulations only | **NOT VERIFIED** |
| Live isolated creation and independent Google events.get | Mock evidence only | **NOT VERIFIED** |
| Cross-store TAL audit and release approval | Shadow audit not atomic with SAFE SQL; warnings not durable | **BLOCKED** |

The [release gate](M5_RELEASE_GATE.md) requires the same reviewed upstream SHA
in both cross-repo workflows and deliberately rejects any live acceptance
request. Passing offline CI does **not** certify Google exactly-once scheduling,
authorization, security, business data use or deployment.
