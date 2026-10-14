# M0 security test matrix

M0 statuses describe **implemented tests**, not guarantees about an unbuilt
live system. Later conformance suites must inspect actual adapter effect counters.

| ID | Check | M0 status | Evidence |
| --- | --- | --- | --- |
| SEC-01 | Reject unknown operation and unknown JSON fields | PASS locally | `test_malformed_or_privileged_proposal_denied` |
| SEC-02 | Prompt text cannot confer an operation grant | Structural only | Closed operation vocabulary; authorization deferred |
| SEC-03 | Reject cross-task/organization planning inputs | PASS locally | `test_runner_rejects_cross_*` |
| SEC-04 | Expired/revoked reference cannot authorize effects | Deferred M1/M2 | Trusted broker not built |
| SEC-05 | Model context excludes raw email/credentials | PASS on synthetic fixture | `test_planner_context_contains_only_scoped_metadata` |
| TAL-01 | Max one effect per cycle | Schema enforces budget=1 | Actual effect-count test deferred M2 |
| TAL-02 | Invalid proposal has zero external effects | No external writer exists | Action gate integration deferred M2 |
| TAL-03 | Approval cannot transfer to other action | Contract includes proposal hash | Runtime approval policy deferred M2 |
| TAL-04 | Model cannot mark task completed | PASS locally | TaskRunner always returns `PROPOSED_UNVERIFIED` |
| SIM-01 | Deterministic synthetic planning | PASS locally | `test_fake_planner_is_deterministic...` |
| SIM-02 | Stale projection cannot authorize a commit | No commit path exists | Freshness checks deferred M1/M2 |
| REP-01 | Persisted event chain replays identically | Not implemented | Upstream TAL does support replay |
| REC-01 | No duplicate effects after unknown outcome | Not implemented | Outbox/reconciliation deferred M4 |
| UPSTREAM-01 | Pinned TAL import matches expected IDs | Optional smoke gate only | Import check skipped without TAL install |

Unimplemented checks **must not** be marked PASS or used to claim enterprise
information-security or compliance certification.
