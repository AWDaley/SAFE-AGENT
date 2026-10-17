# W3: durable validated inference results

Implementation for review in PR #36, stacked on PR #35 at
`b0fd29215900c15be8e0b8d619afd74bccf6b694`. Neither branch is merged by this work.
This is result persistence/recovery, **not W4 operator workflow integration**.

## Changes

`TrustedInferenceDispatchStore.finish` now records one immutable closed result
in the same transaction as the existing RESPONSE_VALIDATED invocation/event.
An insert failure rolls back both; a lost commit acknowledgment is handled by
reading durable state, not by sending inference again. Cost holds and original
invocation transitions are unchanged.

The stored contract binds the validated candidate to its request, task, tenant,
planning lease, manifest, policy, original minimized task/free-busy snapshot,
validation event and time. No original completion or serialized prompt is kept.
Clarification questions are replaced by fixed approved presentation text.
Scoped opaque references necessary for the candidate remain; protected broker
mappings, provider credentials and provider IDs are not added.

`TrustedInferenceCoordinator.resume(principal, lease)` performs no admission or
transport call and needs no caller-supplied prompt. It reconstructs the original
input from the durable typed record. `execute` also reuses an existing available
result rather than returning only its hash. The original source snapshot is
internal data excluded from recovery-receipt serialization, not a new HTTP API.

Recovery statuses:
- AVAILABLE: a bound, integrity-checked candidate exists and the original task,
  planning lease and conservative inference deadline are still current.
- STALE: retain evidence but return no candidate; a stored result does not renew
  any lease, request deadline or projection.
- LEGACY_MISSING: an older hash-only validated invocation has no reusable payload.
  Never fabricate its historical output or dispatch again to reconstruct it.
- NOT_VALIDATED: metadata only for an invocation without validated output.

Recovery uses the original inference deadline as its conservative reuse bound.
This may provide a shorter reuse window than the planning lease. It never extends
that deadline automatically. Pending/UNKNOWN invocation retirement remains under
existing dispatch-recovery and accounting rules.

## Authority boundary

AVAILABLE is **not** an approval, dispatch token or freshness guarantee at future
consumption. W4 must recheck the actual source revision, broker revocation/expiry,
policy and task authority at the preparation/consumption transaction. W3 does not
query a live business source or broker. It also does not yet record final action
consumption. The inference candidate's simulation_pending digest differs from the
later trusted simulated action; approval must bind the latter, never this record.

The database table has foreign keys, attempt uniqueness, validation-event identity
checks and mutation-denying triggers on SQLite and PostgreSQL. Hashes detect
corruption and binding errors, not forgery by an attacker controlling the database
and trusted process. No database-owner, rollback-restore or root-compromise
resistance is claimed. W7 deployment and restore hardening remain necessary.

## Migration and rollout

Forward revision `0012_inference_results` follows `0011_inference_admission`.
No earlier migration changes. No result backfill or financial ledger reset.
Downgrade refuses destructive history removal. Apply the forward migration before
using the new coordinator on a persistent database; missing schema fails closed.

Do not run old and new writers interchangeably during rollout: old code can still
produce hash-only validations. Quiesce writers, migrate, deploy the reviewed
revision, then resume. Historical hash-only results remain non-reusable.

## Tests and evidence

The regression-only commit `96cab331bb4876209e576888870809c8bbe25f9a` was tested
by GitHub Actions run `38830620425`, job `116748311842`:
**2 failed, 611 passed, 27 skipped**. Both failures were the intended lost-result
cases: repository restart and lost finish-commit acknowledgment. Existing tests
passed; later steps of the failing main job were skipped, not reported as passed.
PostgreSQL and pinned TAL interoperability jobs passed separately on that revision.

New acceptance covers exact stored input/candidate recovery, no extra effects or
refunds, expired results, tenant/actor/lease/policy mismatch, malformed/tampered
records, immutable SQL writes, concurrent recovery, result-insert rollback,
clarification sanitization, slow recovery commits and forward migration with
legacy hash-only successes. A dedicated real PostgreSQL job must actually run.
Contract-shape fingerprints are pinned from the generated Pydantic JSON schemas.

Final CI evidence must be recorded against the exact final head in the PR, not
inferred from baseline results or a partial passing job. The author must not
substitute CI for independent review. The full repository checks run in GitHub
Actions; this change does not claim an independent local suite rerun.

## Unchanged release boundary

No hosted inference, real Google access, customer credentials, executable model
tools, automatic refunds or production enablement. M5 live acceptance remains
BLOCKED. W0 governance/build reproducibility and W4+ integration/deployment work
remain outstanding. Keep this PR draft and unmerged pending independent review.
