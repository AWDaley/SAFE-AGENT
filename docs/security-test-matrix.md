# M0–M1 security test matrix

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
